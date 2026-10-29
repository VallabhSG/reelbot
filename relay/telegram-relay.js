// reelbot Telegram relay: a Cloudflare Worker (free plan is plenty).
// Telegram sends every tap/message here the instant it happens. The Worker
//   1. checks it really came from Telegram (secret header) and from your chat,
//   2. stops the button spinner right away ("On it…"),
//   3. stores the update in the private state bucket (inbox/), where the bot reads it,
//   4. starts a GitHub Actions run of the bot (about a minute to act).
// It is also the bot's clock: GitHub's own schedule fires rarely (it skipped almost every run), so
// Cloudflare Cron Triggers (Settings → Triggers, UTC) start the fixed-time runs instead:
//   35 4 * * *  → 10:05 IST, make the episode on episode days
//   2 14 * * *  → 19:32 IST, publish what's due, measure reels, Sunday report
// Bindings (Worker → Settings): R2 bucket INBOX = your state bucket.
// Variables: CHAT_ID, REPO (e.g. VallabhSG/reelbot). Secrets: BOT_TOKEN, WEBHOOK_SECRET, GITHUB_TOKEN.

// Start one "tick" run of the bot's workflow. Returns GitHub's status (204 = started).
async function startRun(env) {
  const r = await fetch(`https://api.github.com/repos/${env.REPO}/actions/workflows/reelbot.yml/dispatches`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.GITHUB_TOKEN}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "reelbot-relay",
    },
    body: JSON.stringify({ ref: "main", inputs: { command: "tick", args: "" } }),
  });
  return r.status;
}

export default {
  async fetch(request, env) {
    if (request.method !== "POST") return new Response("reelbot relay");
    if (request.headers.get("X-Telegram-Bot-Api-Secret-Token") !== env.WEBHOOK_SECRET) {
      return new Response("forbidden", { status: 403 });
    }
    const update = await request.json();
    const chat = update.callback_query?.message?.chat?.id ?? update.message?.chat?.id;
    if (String(chat) !== String(env.CHAT_ID)) return new Response("ignored"); // 200: no retries

    if (update.callback_query) {
      await fetch(`https://api.telegram.org/bot${env.BOT_TOKEN}/answerCallbackQuery`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ callback_query_id: update.callback_query.id, text: "⏳ On it, about a minute…" }),
      }).catch(() => {});
    }

    // If storing fails, answer 500 so Telegram delivers the update again.
    const key = `inbox/${String(update.update_id).padStart(12, "0")}.json`;
    await env.INBOX.put(key, JSON.stringify(update));

    // Kick off a run now. If GitHub is slow or this fails, the next scheduled run still picks it up.
    await startRun(env).catch(() => {});
    return new Response("ok");
  },

  // Cron Triggers: the fixed-time runs. A failed start throws, so it shows in the Worker's logs.
  async scheduled(controller, env, ctx) {
    const status = await startRun(env);
    if (status !== 204) throw new Error(`GitHub did not start the run (HTTP ${status})`);
  },
};
