import Stripe from "npm:stripe@17.7.0";
import { createClient, type SupabaseClient } from "npm:@supabase/supabase-js@2.117.2";

export const stripe = new Stripe(Deno.env.get("STRIPE_SECRET_KEY")!, {
  apiVersion: "2025-02-24.acacia",
  httpClient: Stripe.createFetchHttpClient(),
});

export const SITE_URL = (Deno.env.get("SITE_URL") ?? "").replace(/\/$/, "");

// Clé serveur : nouveaux projets (SUPABASE_SECRET_KEYS, sb_secret_…) ou anciens (service_role).
function serverKey(): string {
  try {
    const keys = JSON.parse(Deno.env.get("SUPABASE_SECRET_KEYS") ?? "{}");
    if (keys.default) return keys.default;
  } catch {
    // variable absente ou illisible : on retombe sur l'ancienne clé
  }
  return Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
}

export const admin: SupabaseClient = createClient(
  Deno.env.get("SUPABASE_URL")!,
  serverKey(),
  { auth: { persistSession: false, autoRefreshToken: false, detectSessionInUrl: false } },
);

// Catalogue. Les prix sont créés dans Stripe au premier appel (lookup_key),
// aucune configuration manuelle du tableau de bord n'est nécessaire.
export const CATALOG = {
  essentiel: { lookup: "gl_essentiel", name: "GhostLoc Essentiel (48 h / mois)", amount: 999, recurring: true },
  illimite: { lookup: "gl_illimite", name: "GhostLoc Illimité", amount: 2999, recurring: true },
  pack10: { lookup: "gl_pack10", name: "GhostLoc pack 10 h", amount: 299, recurring: false, seconds: 10 * 3600 },
  pack25: { lookup: "gl_pack25", name: "GhostLoc pack 25 h", amount: 599, recurring: false, seconds: 25 * 3600 },
} as const;
export type CatalogKey = keyof typeof CATALOG;

export async function priceId(key: CatalogKey): Promise<string> {
  const item = CATALOG[key];
  const found = await stripe.prices.list({ lookup_keys: [item.lookup], active: true, limit: 1 });
  if (found.data[0]) return found.data[0].id;
  const created = await stripe.prices.create({
    currency: "eur",
    unit_amount: item.amount,
    lookup_key: item.lookup,
    transfer_lookup_key: true,
    product_data: { name: item.name },
    ...(item.recurring ? { recurring: { interval: "month" as const } } : {}),
  });
  return created.id;
}

export function planFromSubscription(sub: Stripe.Subscription): "essentiel" | "illimite" | null {
  const lookup = sub.items.data[0]?.price?.lookup_key;
  if (lookup === CATALOG.essentiel.lookup) return "essentiel";
  if (lookup === CATALOG.illimite.lookup) return "illimite";
  const meta = sub.metadata?.plan;
  return meta === "essentiel" || meta === "illimite" ? meta : null;
}

export const cors = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};

export function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { ...cors, "Content-Type": "application/json" } });
}

export async function userFromRequest(req: Request) {
  const token = (req.headers.get("Authorization") ?? "").replace(/^Bearer\s+/i, "");
  if (!token) return null;
  const { data, error } = await admin.auth.getUser(token);
  return error ? null : data.user;
}

export async function ensureCustomer(userId: string, email: string | undefined): Promise<string> {
  const { data } = await admin.from("gl_customers").select("stripe_customer_id").eq("user_id", userId).maybeSingle();
  if (data) return data.stripe_customer_id;
  const customer = await stripe.customers.create({ email, metadata: { user_id: userId } });
  const { error } = await admin.from("gl_customers").insert({ user_id: userId, stripe_customer_id: customer.id });
  if (error) {
    // Course entre deux requêtes : on garde celui qui a été enregistré.
    const again = await admin.from("gl_customers").select("stripe_customer_id").eq("user_id", userId).single();
    return again.data!.stripe_customer_id;
  }
  return customer.id;
}

export function safeReturnUrl(candidate: unknown, fallbackPath: string): string {
  if (typeof candidate === "string" && SITE_URL && candidate.startsWith(SITE_URL + "/")) return candidate;
  return `${SITE_URL}${fallbackPath}`;
}
