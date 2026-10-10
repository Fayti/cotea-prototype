// Paiement : Stripe, Auth et PostgREST simulés via fetch, aucun appel réseau réel.
// deno test --allow-env --allow-net gl-checkout/handler.test.ts
import { assert, assertEquals } from "jsr:@std/assert@1.0.8";

Deno.env.set("STRIPE_SECRET_KEY", "sk_test_x");
Deno.env.set("SUPABASE_URL", "http://supabase.test");
Deno.env.set("SUPABASE_SERVICE_ROLE_KEY", "service");
Deno.env.set("SITE_URL", "https://site.test");

const USER = { id: "11111111-1111-1111-1111-111111111111", email: "a@b.c", aud: "authenticated" };
const db: Record<string, Record<string, unknown>[]> = { gl_customers: [], gl_subscriptions: [] };
const stripeCalls: { path: string; params: URLSearchParams }[] = [];
const prices: Record<string, string> = {};

// deno-lint-ignore require-await
globalThis.fetch = async (input: string | URL | Request, init?: RequestInit) => {
  const url = new URL(typeof input === "string" ? input : input instanceof URL ? input.href : input.url);
  const method = init?.method ?? "GET";
  if (url.hostname === "api.stripe.com") {
    const params = new URLSearchParams(method === "GET" ? url.search : String(init?.body ?? ""));
    stripeCalls.push({ path: `${method} ${url.pathname}`, params });
    if (url.pathname === "/v1/prices" && method === "GET") {
      const key = params.get("lookup_keys[0]")!;
      return Response.json({ object: "list", data: prices[key] ? [{ id: prices[key] }] : [] });
    }
    if (url.pathname === "/v1/prices") {
      const id = "price_" + params.get("lookup_key");
      prices[params.get("lookup_key")!] = id;
      return Response.json({ id });
    }
    if (url.pathname === "/v1/customers") return Response.json({ id: "cus_1" });
    if (url.pathname === "/v1/checkout/sessions") return Response.json({ id: "cs_1", url: "https://checkout.stripe.test/cs_1" });
    if (url.pathname === "/v1/subscriptions/sub_1" && method === "GET") {
      return Response.json({ id: "sub_1", items: { data: [{ id: "si_1" }] } });
    }
    if (url.pathname === "/v1/subscriptions/sub_1") return Response.json({ id: "sub_1" });
    return Response.json({ error: { message: "inattendu " + url.pathname } }, { status: 400 });
  }
  if (url.hostname === "supabase.test") {
    if (url.pathname === "/auth/v1/user") {
      const auth = (init?.headers as Record<string, string>)?.Authorization ?? new Headers(init?.headers).get("Authorization");
      return auth === "Bearer good" ? Response.json(USER) : Response.json({ msg: "bad jwt" }, { status: 401 });
    }
    const table = url.pathname.split("/").pop()!;
    if (method === "GET") {
      const rows = db[table].filter((r) => [...url.searchParams].every(([k, v]) => !v.startsWith("eq.") || String(r[k]) === v.slice(3)));
      return Response.json(rows[0] ?? null, { status: rows[0] ? 200 : 406 });
    }
    db[table].push(JSON.parse(String(init?.body)));
    return new Response(null, { status: 201 });
  }
  throw new Error("réseau interdit : " + url);
};

const { handler } = await import("./handler.ts");

function call(body: unknown, token = "good") {
  return handler(new Request("http://x", { method: "POST", body: JSON.stringify(body), headers: { Authorization: `Bearer ${token}` } }));
}
const lastStripe = (path: string) => [...stripeCalls].reverse().find((c) => c.path === path)!;
const future = new Date(Date.now() + 86400_000 * 20).toISOString();

Deno.test("sans connexion : 401", async () => {
  assertEquals((await call({ item: "essentiel" }, "bad")).status, 401);
});

Deno.test("premier abonnement : session Checkout avec métadonnées et URL de retour du site", async () => {
  const res = await call({ item: "illimite", returnUrl: "https://evil.test/x" });
  assertEquals((await res.json()).url, "https://checkout.stripe.test/cs_1");
  const c = lastStripe("POST /v1/checkout/sessions").params;
  assertEquals(c.get("mode"), "subscription");
  assertEquals(c.get("line_items[0][price]"), "price_gl_illimite");
  assertEquals(c.get("subscription_data[metadata][user_id]"), USER.id);
  assertEquals(c.get("success_url"), "https://site.test/compte.html?paiement=ok"); // URL externe ignorée
  const created = lastStripe("POST /v1/prices").params;
  assertEquals([created.get("unit_amount"), created.get("recurring[interval]")], ["2999", "month"]);
  assertEquals(db.gl_customers.length, 1);
});

Deno.test("pack sans Essentiel actif : refusé", async () => {
  const res = await call({ item: "pack10" });
  assertEquals([res.status, (await res.json()).error], [409, "packs_require_essentiel"]);
});

Deno.test("abonné Essentiel : pack payable, montant 2,99 €", async () => {
  db.gl_subscriptions.push({ user_id: USER.id, stripe_subscription_id: "sub_1", plan: "essentiel", status: "active", period_end: future, cancel_at_period_end: false });
  const res = await call({ item: "pack10" });
  assertEquals(res.status, 200);
  const c = lastStripe("POST /v1/checkout/sessions").params;
  assertEquals([c.get("mode"), c.get("metadata[kind]"), c.get("metadata[seconds]")], ["payment", "pack", "36000"]);
  assertEquals(lastStripe("POST /v1/prices").params.get("unit_amount"), "299");
});

Deno.test("Essentiel → Illimité : abonnement modifié au prorata, pas de nouveau Checkout", async () => {
  const before = stripeCalls.filter((c) => c.path === "POST /v1/checkout/sessions").length;
  const res = await call({ item: "illimite" });
  assertEquals(await res.json(), { updated: true });
  const u = lastStripe("POST /v1/subscriptions/sub_1").params;
  assertEquals([u.get("items[0][id]"), u.get("items[0][price]"), u.get("proration_behavior")], ["si_1", "price_gl_illimite", "always_invoice"]);
  assertEquals(stripeCalls.filter((c) => c.path === "POST /v1/checkout/sessions").length, before);
});

Deno.test("même formule : déjà abonné, ou réactivation si résiliation programmée", async () => {
  assertEquals((await call({ item: "essentiel" })).status, 409);
  db.gl_subscriptions[0].cancel_at_period_end = true;
  assertEquals(await (await call({ item: "essentiel" })).json(), { updated: true });
  assertEquals(lastStripe("POST /v1/subscriptions/sub_1").params.get("cancel_at_period_end"), "false");
  assert(!lastStripe("POST /v1/subscriptions/sub_1").params.has("items[0][price]"));
});
