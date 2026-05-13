import "jsr:@supabase/functions-js/edge-runtime.d.ts";

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StreamableHTTPTransport } from "@hono/mcp";
import { Hono } from "hono";
import { z } from "zod";
import { createClient } from "@supabase/supabase-js";

const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
const SUPABASE_SERVICE_ROLE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
const MCP_ACCESS_KEY = Deno.env.get("MCP_ACCESS_KEY")!;
const OLLAMA_BASE = Deno.env.get("OLLAMA_BASE") || "http://localhost:11434";
const OLLAMA_EMBED_MODEL = Deno.env.get("OLLAMA_EMBED_MODEL") || "mxbai-embed-large";
const OLLAMA_CHAT_MODEL = Deno.env.get("OLLAMA_CHAT_MODEL") || "gpt-oss-20b";

const supabase = createClient(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY);

async function getEmbedding(text: string): Promise<number[]> {
  const r = await fetch(`${OLLAMA_BASE}/api/embed`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      model: OLLAMA_EMBED_MODEL,
      input: text,
    }),
  });
  if (!r.ok) {
    const msg = await r.text().catch(() => "");
    throw new Error(`Ollama embeddings failed: ${r.status} ${msg}`);
  }
  const d = await r.json();
  return d.embeddings[0];
}

async function extractMetadata(text: string): Promise<Record<string, unknown>> {
  const r = await fetch(`${OLLAMA_BASE}/api/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      model: OLLAMA_CHAT_MODEL,
      format: "json",
      stream: false,
      messages: [
        {
          role: "system",
          content: `Extract metadata from the user's captured thought. Return JSON with:
- "people": array of people mentioned (empty if none)
- "action_items": array of implied to-dos (empty if none)
- "dates_mentioned": array of dates YYYY-MM-DD (empty if none)
- "topics": array of 1-3 short topic tags (always at least one)
- "type": one of "observation", "task", "idea", "reference", "person_note"
Only extract what's explicitly there.`,
        },
        { role: "user", content: text },
      ],
    }),
  });
  const d = await r.json();
  try {
    return JSON.parse(d.message.content);
  } catch {
    return { topics: ["uncategorized"], type: "observation" };
  }
}

// --- MCP Server Setup ---

const server = new McpServer({
  name: "open-brain",
  version: "1.0.0",
});

// Tool 1: Semantic Search
server.registerTool(
  "search_thoughts",
  {
    title: "Search Thoughts",
    description:
      "Search captured thoughts by meaning. Use this when the user asks about a topic, person, or idea they've previously captured.",
    inputSchema: {
      query: z.string().describe("What to search for"),
      limit: z.number().optional().default(10),
      threshold: z.number().optional().default(0.5),
      include_flagged: z
        .boolean()
        .optional()
        .default(false)
        .describe("If true, include thoughts flagged for removal"),
    },
  },
  async ({ query, limit, threshold, include_flagged }) => {
    try {
      const qEmb = await getEmbedding(query);
      const { data, error } = await supabase.rpc("match_thoughts", {
        query_embedding: qEmb,
        match_threshold: threshold,
        match_count: limit,
        filter: {},
        include_flagged,
      });

      if (error) {
        return {
          content: [{ type: "text" as const, text: `Search error: ${error.message}` }],
          isError: true,
        };
      }

      if (!data || data.length === 0) {
        return {
          content: [{ type: "text" as const, text: `No thoughts found matching "${query}".` }],
        };
      }

      const results = data.map(
        (
          t: {
            id: string;
            content: string;
            metadata: Record<string, unknown>;
            similarity: number;
            created_at: string;
          },
          i: number
        ) => {
          const m = t.metadata || {};
          const parts = [
            `--- Result ${i + 1} (${(t.similarity * 100).toFixed(1)}% match) id=${t.id} ---`,
            `Captured: ${new Date(t.created_at).toLocaleDateString()}`,
            `Type: ${m.type || "unknown"}`,
          ];
          if (Array.isArray(m.topics) && m.topics.length)
            parts.push(`Topics: ${(m.topics as string[]).join(", ")}`);
          if (Array.isArray(m.people) && m.people.length)
            parts.push(`People: ${(m.people as string[]).join(", ")}`);
          if (Array.isArray(m.action_items) && m.action_items.length)
            parts.push(`Actions: ${(m.action_items as string[]).join("; ")}`);
          if (Array.isArray(m.sources) && m.sources.length)
            parts.push(
              `Sources: ${(m.sources as Array<{ system: string; locator: string }>)
                .map((s) => `${s.system}:${s.locator}`)
                .join(", ")}`,
            );
          parts.push(`\n${t.content}`);
          return parts.join("\n");
        }
      );

      return {
        content: [
          {
            type: "text" as const,
            text: `Found ${data.length} thought(s):\n\n${results.join("\n\n")}`,
          },
        ],
      };
    } catch (err: unknown) {
      return {
        content: [{ type: "text" as const, text: `Error: ${(err as Error).message}` }],
        isError: true,
      };
    }
  }
);

// Tool 2: List Recent
server.registerTool(
  "list_thoughts",
  {
    title: "List Recent Thoughts",
    description:
      "List recently captured thoughts with optional filters by type, topic, person, or time range.",
    inputSchema: {
      limit: z.number().optional().default(10),
      type: z.string().optional().describe("Filter by type: observation, task, idea, reference, person_note"),
      topic: z.string().optional().describe("Filter by topic tag"),
      person: z.string().optional().describe("Filter by person mentioned"),
      days: z.number().optional().describe("Only thoughts from the last N days"),
      include_flagged: z
        .boolean()
        .optional()
        .default(false)
        .describe("If true, include thoughts flagged for removal"),
    },
  },
  async ({ limit, type, topic, person, days, include_flagged }) => {
    try {
      let q = supabase
        .from("thoughts")
        .select("id, content, metadata, created_at, to_be_deleted")
        .order("created_at", { ascending: false })
        .limit(limit);

      if (!include_flagged) q = q.eq("to_be_deleted", false);
      if (type) q = q.contains("metadata", { type });
      if (topic) q = q.contains("metadata", { topics: [topic] });
      if (person) q = q.contains("metadata", { people: [person] });
      if (days) {
        const since = new Date();
        since.setDate(since.getDate() - days);
        q = q.gte("created_at", since.toISOString());
      }

      const { data, error } = await q;

      if (error) {
        return {
          content: [{ type: "text" as const, text: `Error: ${error.message}` }],
          isError: true,
        };
      }

      if (!data || !data.length) {
        return { content: [{ type: "text" as const, text: "No thoughts found." }] };
      }

      const results = data.map(
        (
          t: {
            id: string;
            content: string;
            metadata: Record<string, unknown>;
            created_at: string;
            to_be_deleted: boolean;
          },
          i: number
        ) => {
          const m = t.metadata || {};
          const tags = Array.isArray(m.topics) ? (m.topics as string[]).join(", ") : "";
          const flagMark = t.to_be_deleted ? " [FLAGGED]" : "";
          const srcs =
            Array.isArray(m.sources) && m.sources.length
              ? `\n   Sources: ${(m.sources as Array<{ system: string; locator: string }>)
                  .map((s) => `${s.system}:${s.locator}`)
                  .join(", ")}`
              : "";
          return `${i + 1}. [${new Date(t.created_at).toLocaleDateString()}] id=${t.id}${flagMark} (${m.type || "??"}${tags ? " - " + tags : ""})\n   ${t.content}${srcs}`;
        }
      );

      return {
        content: [
          {
            type: "text" as const,
            text: `${data.length} recent thought(s):\n\n${results.join("\n\n")}`,
          },
        ],
      };
    } catch (err: unknown) {
      return {
        content: [{ type: "text" as const, text: `Error: ${(err as Error).message}` }],
        isError: true,
      };
    }
  }
);

// Tool 3: Stats
server.registerTool(
  "thought_stats",
  {
    title: "Thought Statistics",
    description: "Get a summary of all captured thoughts: totals, types, top topics, and people.",
    inputSchema: {},
  },
  async () => {
    try {
      const { count } = await supabase
        .from("thoughts")
        .select("*", { count: "exact", head: true });

      const { data } = await supabase
        .from("thoughts")
        .select("metadata, created_at")
        .order("created_at", { ascending: false });

      const types: Record<string, number> = {};
      const topics: Record<string, number> = {};
      const people: Record<string, number> = {};

      for (const r of data || []) {
        const m = (r.metadata || {}) as Record<string, unknown>;
        if (m.type) types[m.type as string] = (types[m.type as string] || 0) + 1;
        if (Array.isArray(m.topics))
          for (const t of m.topics) topics[t as string] = (topics[t as string] || 0) + 1;
        if (Array.isArray(m.people))
          for (const p of m.people) people[p as string] = (people[p as string] || 0) + 1;
      }

      const sort = (o: Record<string, number>): [string, number][] =>
        Object.entries(o)
          .sort((a, b) => b[1] - a[1])
          .slice(0, 10);

      const lines: string[] = [
        `Total thoughts: ${count}`,
        `Date range: ${
          data?.length
            ? new Date(data[data.length - 1].created_at).toLocaleDateString() +
              " → " +
              new Date(data[0].created_at).toLocaleDateString()
            : "N/A"
        }`,
        "",
        "Types:",
        ...sort(types).map(([k, v]) => `  ${k}: ${v}`),
      ];

      if (Object.keys(topics).length) {
        lines.push("", "Top topics:");
        for (const [k, v] of sort(topics)) lines.push(`  ${k}: ${v}`);
      }

      if (Object.keys(people).length) {
        lines.push("", "People mentioned:");
        for (const [k, v] of sort(people)) lines.push(`  ${k}: ${v}`);
      }

      return { content: [{ type: "text" as const, text: lines.join("\n") }] };
    } catch (err: unknown) {
      return {
        content: [{ type: "text" as const, text: `Error: ${(err as Error).message}` }],
        isError: true,
      };
    }
  }
);

// Tool 4: Capture Thought
server.registerTool(
  "capture_thought",
  {
    title: "Capture Thought",
    description:
      "Save a new thought to the Open Brain. Generates an embedding and extracts metadata automatically. Use this when the user wants to save something to their brain directly from any AI client — notes, insights, decisions, or migrated content from other systems.",
    inputSchema: {
      content: z.string().describe("The thought to capture — a clear, standalone statement that will make sense when retrieved later by any AI"),
      sources: z
        .array(
          z.object({
            system: z.string().min(1).describe("Source system identifier (e.g., 'obsidian', 'web', 'gmail', 'slack')"),
            locator: z.string().min(1).describe("Unique identifier within the source system (file path, URL, message id, etc.)"),
            label: z.string().optional().describe("Optional human-readable label"),
            extra: z.record(z.unknown()).optional().describe("Optional system-specific metadata"),
          }),
        )
        .optional()
        .describe("Optional origins this thought derives from. Each entry is uniquely identified by (system, locator)."),
    },
  },
  async ({ content, sources }) => {
    try {
      const [embedding, metadata] = await Promise.all([
        getEmbedding(content),
        extractMetadata(content),
      ]);

      const finalMetadata: Record<string, unknown> = { ...metadata, source: "mcp" };
      if (sources && sources.length > 0) finalMetadata.sources = sources;

      const { error } = await supabase.from("thoughts").insert({
        content,
        embedding,
        metadata: finalMetadata,
      });

      if (error) {
        return {
          content: [{ type: "text" as const, text: `Failed to capture: ${error.message}` }],
          isError: true,
        };
      }

      const meta = metadata as Record<string, unknown>;
      let confirmation = `Captured as ${meta.type || "thought"}`;
      if (Array.isArray(meta.topics) && meta.topics.length)
        confirmation += ` — ${(meta.topics as string[]).join(", ")}`;
      if (Array.isArray(meta.people) && meta.people.length)
        confirmation += ` | People: ${(meta.people as string[]).join(", ")}`;
      if (Array.isArray(meta.action_items) && meta.action_items.length)
        confirmation += ` | Actions: ${(meta.action_items as string[]).join("; ")}`;
      if (sources && sources.length > 0)
        confirmation += ` | Sources: ${sources.map((s) => `${s.system}:${s.locator}`).join(", ")}`;

      return {
        content: [{ type: "text" as const, text: confirmation }],
      };
    } catch (err: unknown) {
      return {
        content: [{ type: "text" as const, text: `Error: ${(err as Error).message}` }],
        isError: true,
      };
    }
  }
);

// Tool 5: Re-embed Pending
server.registerTool(
  "reembed_pending",
  {
    title: "Re-embed Pending Thoughts",
    description:
      "Find thoughts with NULL embeddings (edited or never embedded) and regenerate them. Processes up to `batch_size` rows per call. Returns counts so a client can loop until drained.",
    inputSchema: {
      batch_size: z
        .number()
        .int()
        .min(1)
        .max(500)
        .optional()
        .default(50)
        .describe("Max rows to process in this call"),
      dry_run: z
        .boolean()
        .optional()
        .default(false)
        .describe("If true, report counts without writing"),
    },
  },
  async ({ batch_size, dry_run }) => {
    try {
      const { count: remainingBefore, error: countErr } = await supabase
        .from("thoughts")
        .select("*", { count: "exact", head: true })
        .is("embedding", null)
        .eq("to_be_deleted", false);

      if (countErr) {
        return {
          content: [{ type: "text" as const, text: `Count error: ${countErr.message}` }],
          isError: true,
        };
      }

      if (dry_run) {
        return {
          content: [
            {
              type: "text" as const,
              text: JSON.stringify({
                processed: 0,
                failed: [],
                remaining: remainingBefore ?? 0,
                dry_run: true,
              }),
            },
          ],
        };
      }

      const { data: rows, error: selectErr } = await supabase
        .from("thoughts")
        .select("id, content")
        .is("embedding", null)
        .eq("to_be_deleted", false)
        .order("id", { ascending: true })
        .limit(batch_size);

      if (selectErr) {
        return {
          content: [{ type: "text" as const, text: `Select error: ${selectErr.message}` }],
          isError: true,
        };
      }

      let processed = 0;
      const failed: { id: number; error: string }[] = [];

      for (const row of rows || []) {
        try {
          const embedding = await getEmbedding(row.content);
          const { error: updateErr } = await supabase
            .from("thoughts")
            .update({ embedding })
            .eq("id", row.id);
          if (updateErr) {
            failed.push({ id: row.id, error: updateErr.message });
          } else {
            processed++;
          }
        } catch (err: unknown) {
          failed.push({ id: row.id, error: (err as Error).message });
        }
      }

      const remaining = Math.max(0, (remainingBefore ?? 0) - processed);

      return {
        content: [
          {
            type: "text" as const,
            text: JSON.stringify({
              processed,
              failed,
              remaining,
              dry_run: false,
            }),
          },
        ],
      };
    } catch (err: unknown) {
      return {
        content: [{ type: "text" as const, text: `Error: ${(err as Error).message}` }],
        isError: true,
      };
    }
  }
);

// Tool 6: Update Thought
server.registerTool(
  "update_thought",
  {
    title: "Update Thought",
    description:
      "Edit the content of an existing thought and re-embed it atomically. Use this when the user wants to revise or correct a previously captured thought without losing its id, metadata, or creation timestamp.",
    inputSchema: {
      id: z.string().describe("UUID of the thought to update"),
      content: z.string().describe("New content for the thought"),
    },
  },
  async ({ id, content }) => {
    try {
      const embedding = await getEmbedding(content);

      const { data, error } = await supabase
        .from("thoughts")
        .update({ content, embedding })
        .eq("id", id)
        .select("id");

      if (error) {
        return {
          content: [{ type: "text" as const, text: `Update error: ${error.message}` }],
          isError: true,
        };
      }
      if (!data || data.length === 0) {
        return {
          content: [{ type: "text" as const, text: `No thought found with id ${id}` }],
          isError: true,
        };
      }

      return {
        content: [
          { type: "text" as const, text: `Updated thought ${id} (re-embedded ${content.length} chars).` },
        ],
      };
    } catch (err: unknown) {
      return {
        content: [{ type: "text" as const, text: `Error: ${(err as Error).message}` }],
        isError: true,
      };
    }
  }
);

// Tool 7: Flag Thought for Removal
server.registerTool(
  "flag_thought_for_removal",
  {
    title: "Flag Thought for Removal",
    description:
      "Mark or unmark a thought as flagged for removal. Flagged thoughts are excluded from search and list results by default but remain in the database until the user manually deletes them.",
    inputSchema: {
      id: z.string().describe("UUID of the thought to flag"),
      flagged: z
        .boolean()
        .optional()
        .default(true)
        .describe("Set to false to unflag a previously-flagged thought"),
    },
  },
  async ({ id, flagged }) => {
    try {
      const { data, error } = await supabase
        .from("thoughts")
        .update({ to_be_deleted: flagged })
        .eq("id", id)
        .select("id");

      if (error) {
        return {
          content: [{ type: "text" as const, text: `Update error: ${error.message}` }],
          isError: true,
        };
      }
      if (!data || data.length === 0) {
        return {
          content: [{ type: "text" as const, text: `No thought found with id ${id}` }],
          isError: true,
        };
      }

      return {
        content: [
          {
            type: "text" as const,
            text: flagged ? `Flagged thought ${id} for removal.` : `Unflagged thought ${id}.`,
          },
        ],
      };
    } catch (err: unknown) {
      return {
        content: [{ type: "text" as const, text: `Error: ${(err as Error).message}` }],
        isError: true,
      };
    }
  }
);

// Tool 8: Find All Conflicts (corpus-wide)
server.registerTool(
  "find_all_conflicts",
  {
    title: "Find All Conflicts",
    description:
      "Scan the entire thoughts corpus for pairs of thoughts whose semantic similarity is at or above the given threshold. Returns canonical pairs (each pair appears once) ordered by similarity descending. Use this to surface near-duplicates and likely contradictions for interactive review.",
    inputSchema: {
      threshold: z
        .number()
        .min(0)
        .max(1)
        .optional()
        .default(0.75)
        .describe("Cosine similarity threshold (0-1). Higher = stricter."),
      include_flagged: z
        .boolean()
        .optional()
        .default(false)
        .describe("If true, include pairs where either thought is flagged for removal"),
      limit: z
        .number()
        .int()
        .min(1)
        .max(10000)
        .optional()
        .default(1000)
        .describe("Max pairs to return (truncated from the top of the similarity-ordered list)"),
    },
  },
  async ({ threshold, include_flagged, limit }) => {
    try {
      const { data, error } = await supabase.rpc("find_conflicts", {
        similarity_threshold: threshold,
        include_flagged,
      });

      if (error) {
        return {
          content: [{ type: "text" as const, text: `RPC error: ${error.message}` }],
          isError: true,
        };
      }

      const allPairs = (data || []) as Array<Record<string, unknown>>;
      const returned = allPairs.slice(0, limit);

      return {
        content: [
          {
            type: "text" as const,
            text: JSON.stringify({
              total_pairs: allPairs.length,
              returned: returned.length,
              threshold,
              include_flagged,
              pairs: returned,
            }),
          },
        ],
      };
    } catch (err: unknown) {
      return {
        content: [{ type: "text" as const, text: `Error: ${(err as Error).message}` }],
        isError: true,
      };
    }
  }
);

// --- Hono App with Auth + CORS ---

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type, x-brain-key, accept, mcp-session-id",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS, DELETE",
};

const app = new Hono();

// CORS preflight — required for browser/Electron-based clients (Claude Desktop, claude.ai)
app.options("*", (c) => {
  return c.text("ok", 200, corsHeaders);
});

app.all("*", async (c) => {
  // Accept access key via header OR URL query parameter
  const provided = c.req.header("x-brain-key") || new URL(c.req.url).searchParams.get("key");
  if (!provided || provided !== MCP_ACCESS_KEY) {
    return c.json({ error: "Invalid or missing access key" }, 401, corsHeaders);
  }

  // Fix: Claude Desktop connectors don't send the Accept header that
  // StreamableHTTPTransport requires. Build a patched request if missing.
  // See: https://github.com/NateBJones-Projects/OB1/issues/33
  if (!c.req.header("accept")?.includes("text/event-stream")) {
    const headers = new Headers(c.req.raw.headers);
    headers.set("Accept", "application/json, text/event-stream");
    const patched = new Request(c.req.raw.url, {
      method: c.req.raw.method,
      headers,
      body: c.req.raw.body,
      // @ts-ignore -- duplex required for streaming body in Deno
      duplex: "half",
    });
    Object.defineProperty(c.req, "raw", { value: patched, writable: true });
  }

  const transport = new StreamableHTTPTransport();
  await server.connect(transport);
  return transport.handleRequest(c);
});

Deno.serve(app.fetch);
