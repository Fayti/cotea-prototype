import Stripe from "npm:stripe@17.7.0";
import { admin, planFromSubscription, stripe } from "../_shared/stripe.ts";

const cryptoProvider = Stripe.createSubtleCryptoProvider();
const ACTIVE = ["active", "trialing", "past_due"];

function periodOf(sub: Stripe.Subscription) {
  // Selon la version d'API, la période est sur l'abonnement ou sur l'élément.
  // deno-lint-ignore no-explicit-any
  const s = sub as any;
  const start = s.current_period_start ?? s.items?.data?.[0]?.current_period_start;
  const end = s.current_period_end ?? s.items?.data?.[0]?.current_period_end;
  return { start: new Date(start * 1000).toISOString(), end: new Date(end * 1000).toISOString() };
}

async function userIdFor(sub: Stripe.Subscription): Promise<string | null> {
  if (sub.metadata?.user_id) return sub.metadata.user_id;
  const customer = typeof sub.customer === "string" ? sub.customer : sub.customer.id;
  const { data } = await admin.from("gl_customers").select("user_id").eq("stripe_customer_id", customer).maybeSingle();
  return data?.user_id ?? null;
}

async function syncSubscription(subId: string) {
  const sub = await stripe.subscriptions.retrieve(subId);
  const userId = await userIdFor(sub);
  const plan = planFromSubscription(sub);
  if (!userId || !plan) {
    console.warn("abonnement ignoré", subId, userId, plan);
    return;
  }
  const { data: existing } = await admin.from("gl_subscriptions").select("*").eq("user_id", userId).maybeSingle();
  // Un ancien abonnement terminé ne doit pas écraser un abonnement plus récent encore actif.
  if (existing && existing.stripe_subscription_id !== sub.id && ACTIVE.includes(existing.status) && !ACTIVE.includes(sub.status)) {
    return;
  }
  const period = periodOf(sub);
  const { error } = await admin.from("gl_subscriptions").upsert({
    user_id: userId,
    stripe_subscription_id: sub.id,
    plan,
    status: sub.status,
    period_start: period.start,
    period_end: period.end,
    cancel_at_period_end: sub.cancel_at_period_end,
    updated_at: new Date().toISOString(),
  }, { onConflict: "user_id" });
  if (error) throw error;
}

async function creditPack(session: Stripe.Checkout.Session) {
  if (session.payment_status !== "paid") return;
  const userId = session.metadata?.user_id;
  const seconds = Number(session.metadata?.seconds);
  if (!userId || !seconds) return;
  const { data: sub } = await admin.from("gl_subscriptions").select("period_start").eq("user_id", userId).maybeSingle();
  if (!sub) {
    console.error("pack payé sans abonnement", session.id);
    return;
  }
  const { error } = await admin.from("gl_hour_packs").upsert(
    { user_id: userId, seconds, period_start: sub.period_start, stripe_session_id: session.id },
    { onConflict: "stripe_session_id", ignoreDuplicates: true },
  );
  if (error) throw error;
}

export async function handler(req: Request): Promise<Response> {
  const signature = req.headers.get("Stripe-Signature");
  const body = await req.text();
  let event: Stripe.Event;
  try {
    event = await stripe.webhooks.constructEventAsync(
      body, signature!, Deno.env.get("STRIPE_WEBHOOK_SECRET")!, undefined, cryptoProvider,
    );
  } catch (e) {
    return new Response(`signature invalide: ${(e as Error).message}`, { status: 400 });
  }

  try {
    switch (event.type) {
      case "checkout.session.completed":
      case "checkout.session.async_payment_succeeded": {
        const session = event.data.object as Stripe.Checkout.Session;
        if (session.mode === "subscription" && session.subscription) {
          await syncSubscription(typeof session.subscription === "string" ? session.subscription : session.subscription.id);
        } else if (session.metadata?.kind === "pack") {
          await creditPack(session);
        }
        break;
      }
      case "customer.subscription.created":
      case "customer.subscription.updated":
      case "customer.subscription.deleted":
        await syncSubscription((event.data.object as Stripe.Subscription).id);
        break;
      case "invoice.paid": {
        const invoice = event.data.object as Stripe.Invoice;
        if (invoice.subscription) {
          await syncSubscription(typeof invoice.subscription === "string" ? invoice.subscription : invoice.subscription.id);
        }
        break;
      }
    }
  } catch (e) {
    console.error(event.type, e);
    return new Response("erreur de traitement", { status: 500 });
  }
  return new Response(JSON.stringify({ received: true }), { headers: { "Content-Type": "application/json" } });
}
