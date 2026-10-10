// Webhook Stripe de bout en bout, sans réseau : l'API Stripe et PostgREST sont simulés via fetch.
// deno test --allow-env --allow-net gl-stripe-webhook/handler.test.ts
import { assertEquals } from "jsr:@std/assert@1.0.8";

Deno.env.set("STRIPE_SECRET_KEY", "sk_test_x");
Deno.env.set("STRIPE_WEBHOOK_SECRET", "whsec_test");
Deno.env.set("SUPABASE_URL", "http://supabase.test");
Deno.env.set("SUPABASE_SERVICE_ROLE_KEY", "service");
Deno.env.set("SITE_URL", "https://site.test");

const USER = "11111111-1111-1111-1111-111111111111";
const db: Record<string, Record<string, unknown>[]> = { gl_customers: [], gl_subscriptions: [], gl_hour_packs: [] };
const writes: { table: string; body: unknown }[] = [];

function subscription(overrides: Record<string, unknown> = {}) {
  return {
    id: "sub_1", object: "subscription", customer: "cus_1", status: "active", cancel_at_period_end: false,
    current_period_start: 1_790_000_000, current_period_end: 1_792_592_000, metadata: { user_id: USER, plan: "essentiel" },
    items: { object: "list", data: [{ id: "si_1", price: { id: "price_e", lookup_key: "gl_essentiel" } }] },
    ...overrides,
  };
}
let stripeSub = subscription();

// deno-lint-ignore require-await
globalThis.fetch = async (input: string | URL | Request, init?: RequestInit) => {
  const url = new URL(typeof input === "string" ? input : input instanceof URL ? input.href : input.url);
  const method = init?.method ?? (input instanceof Request ? input.method : "GET");
  if (url.hostname === "api.stripe.com") {
    if (url.pathname === "/v1/subscriptions/sub_1") return Response.json(stripeSub);
    return Response.json({ error: { message: "inattendu " + url.pathname } }, { status: 404 });
  }
  if (url.hostname === "supabase.test") {
    const table = url.pathname.split("/").pop()!;
    if (method === "GET") {
      const rows = db[table].filter((r) =>
        [...url.searchParams].every(([k, v]) => !v.startsWith("eq.") || String(r[k]) === v.slice(3))
      );
      const single = (init?.headers as Record<string, string> | undefined)?.Accept?.includes("vnd.pgrst.object");
      return single ? Response.json(rows[0] ?? null, { status: rows[0] ? 200 : 406 }) : Response.json(rows);
    }
    const body = JSON.parse(String(init?.body));
    writes.push({ table, body });
    const row = Array.isArray(body) ? body[0] : body;
    const key = table === "gl_hour_packs" ? "stripe_session_id" : "user_id";
    const existing = db[table].findIndex((r) => r[key] === row[key]);
    if (existing >= 0 && table !== "gl_hour_packs") db[table][existing] = row;
    else if (existing < 0) db[table].push(row);
    return new Response(null, { status: 201 });
  }
  throw new Error("réseau interdit : " + url);
};

const { handler } = await import("./handler.ts");
const { stripe } = await import("../_shared/stripe.ts");

async function send(type: string, object: unknown, secret = "whsec_test") {
  const payload = JSON.stringify({ id: "evt_" + crypto.randomUUID(), object: "event", type, data: { object } });
  const header = await stripe.webhooks.generateTestHeaderStringAsync({ payload, secret });
  return handler(new Request("http://x", { method: "POST", body: payload, headers: { "Stripe-Signature": header } }));
}

Deno.test("signature invalide refusée", async () => {
  const res = await send("customer.subscription.updated", stripeSub, "whsec_autre");
  assertEquals(res.status, 400);
  assertEquals(writes.length, 0);
});

Deno.test("abonnement payé : Essentiel enregistré avec sa période", async () => {
  const res = await send("checkout.session.completed", { id: "cs_1", object: "checkout.session", mode: "subscription", subscription: "sub_1", metadata: {} });
  assertEquals(res.status, 200);
  const row = db.gl_subscriptions[0];
  assertEquals([row.user_id, row.plan, row.status], [USER, "essentiel", "active"]);
  assertEquals(row.period_start, new Date(1_790_000_000 * 1000).toISOString());
});

Deno.test("pack payé : 10 h créditées une seule fois sur la période en cours", async () => {
  const pack = { id: "cs_pack", object: "checkout.session", mode: "payment", payment_status: "paid", metadata: { user_id: USER, kind: "pack", seconds: "36000" } };
  await send("checkout.session.completed", pack);
  await send("checkout.session.completed", pack); // Stripe peut renvoyer un événement
  assertEquals(db.gl_hour_packs.length, 1);
  assertEquals(db.gl_hour_packs[0].seconds, 36000);
  assertEquals(db.gl_hour_packs[0].period_start, db.gl_subscriptions[0].period_start);
});

Deno.test("passage à l'Illimité puis résiliation", async () => {
  stripeSub = subscription({ items: { object: "list", data: [{ id: "si_1", price: { id: "price_i", lookup_key: "gl_illimite" } }] } });
  await send("customer.subscription.updated", stripeSub);
  assertEquals(db.gl_subscriptions[0].plan, "illimite");
  stripeSub = subscription({ status: "canceled", items: stripeSub.items });
  await send("customer.subscription.deleted", stripeSub);
  assertEquals(db.gl_subscriptions[0].status, "canceled");
});
