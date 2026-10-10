import {
  admin, CATALOG, type CatalogKey, cors, ensureCustomer, json, priceId, safeReturnUrl, stripe, userFromRequest,
} from "../_shared/stripe.ts";

const ACTIVE = ["active", "trialing", "past_due"];

export async function handler(req: Request): Promise<Response> {
  if (req.method === "OPTIONS") return new Response("ok", { headers: cors });
  if (req.method !== "POST") return json({ error: "method_not_allowed" }, 405);

  const user = await userFromRequest(req);
  if (!user) return json({ error: "not_authenticated" }, 401);

  const body = await req.json().catch(() => ({}));
  const item = body.item as CatalogKey;
  if (!(item in CATALOG)) return json({ error: "unknown_item" }, 400);
  const returnUrl = safeReturnUrl(body.returnUrl, "/compte.html");

  try {
    const customer = await ensureCustomer(user.id, user.email);
    const { data: sub } = await admin.from("gl_subscriptions").select("*").eq("user_id", user.id).maybeSingle();
    const subActive = !!sub && ACTIVE.includes(sub.status) && new Date(sub.period_end) > new Date();
    const price = await priceId(item);

    if (item === "pack10" || item === "pack25") {
      if (!subActive || sub.plan !== "essentiel") return json({ error: "packs_require_essentiel" }, 409);
      const session = await stripe.checkout.sessions.create({
        mode: "payment",
        customer,
        client_reference_id: user.id,
        line_items: [{ price, quantity: 1 }],
        metadata: { user_id: user.id, kind: "pack", seconds: String(CATALOG[item].seconds) },
        success_url: `${returnUrl}?paiement=ok`,
        cancel_url: `${returnUrl}?paiement=annule`,
      });
      return json({ url: session.url });
    }

    if (subActive) {
      if (sub.plan === item) {
        if (!sub.cancel_at_period_end) return json({ error: "already_subscribed" }, 409);
        // Résiliation programmée : recliquer sur la même formule la réactive.
        await stripe.subscriptions.update(sub.stripe_subscription_id, { cancel_at_period_end: false });
        return json({ updated: true });
      }
      // Changement de formule : on modifie l'abonnement existant, au prorata.
      const current = await stripe.subscriptions.retrieve(sub.stripe_subscription_id);
      await stripe.subscriptions.update(current.id, {
        items: [{ id: current.items.data[0].id, price }],
        proration_behavior: "always_invoice",
        cancel_at_period_end: false,
        metadata: { user_id: user.id, plan: item },
      });
      return json({ updated: true });
    }

    const session = await stripe.checkout.sessions.create({
      mode: "subscription",
      customer,
      client_reference_id: user.id,
      line_items: [{ price, quantity: 1 }],
      subscription_data: { metadata: { user_id: user.id, plan: item } },
      allow_promotion_codes: true,
      success_url: `${returnUrl}?paiement=ok`,
      cancel_url: `${returnUrl}?paiement=annule`,
    });
    return json({ url: session.url });
  } catch (e) {
    console.error(e);
    return json({ error: "stripe_error", message: (e as Error).message }, 502);
  }
}
