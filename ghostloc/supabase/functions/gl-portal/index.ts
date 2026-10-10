import { cors, ensureCustomer, json, safeReturnUrl, stripe, userFromRequest } from "../_shared/stripe.ts";

// Portail client Stripe : résiliation, moyen de paiement, factures.
// La configuration du portail est créée au premier appel.
async function portalConfiguration(): Promise<string> {
  const list = await stripe.billingPortal.configurations.list({ active: true, limit: 100 });
  const mine = list.data.find((c) => c.metadata?.app === "ghostloc");
  if (mine) return mine.id;
  const created = await stripe.billingPortal.configurations.create({
    metadata: { app: "ghostloc" },
    business_profile: { headline: "GhostLoc — gère ton abonnement" },
    features: {
      invoice_history: { enabled: true },
      payment_method_update: { enabled: true },
      customer_update: { enabled: true, allowed_updates: ["email", "address"] },
      subscription_cancel: { enabled: true, mode: "at_period_end", cancellation_reason: { enabled: true, options: ["too_expensive", "unused", "other"] } },
    },
  });
  return created.id;
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: cors });
  const user = await userFromRequest(req);
  if (!user) return json({ error: "not_authenticated" }, 401);
  const body = await req.json().catch(() => ({}));
  try {
    const session = await stripe.billingPortal.sessions.create({
      customer: await ensureCustomer(user.id, user.email),
      configuration: await portalConfiguration(),
      return_url: safeReturnUrl(body.returnUrl, "/compte.html"),
    });
    return json({ url: session.url });
  } catch (e) {
    console.error(e);
    return json({ error: "stripe_error", message: (e as Error).message }, 502);
  }
});
