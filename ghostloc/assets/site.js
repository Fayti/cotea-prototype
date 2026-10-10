// Configuration + client Supabase partagés par les pages du site.
window.GL = (() => {
  let cfg = null, client = null;
  async function config() {
    if (!cfg) cfg = await (await fetch("config.json", {cache: "no-store"})).json();
    return cfg;
  }
  async function sb() {
    if (client) return client;
    const c = await config();
    if (!c.supabaseUrl || c.supabaseUrl.includes("YOUR-PROJECT")) return null;
    client = window.supabase.createClient(c.supabaseUrl, c.supabaseAnonKey, {auth: {persistSession: true, detectSessionInUrl: true}});
    return client;
  }
  function toast(text, err) {
    let t = document.getElementById("toast");
    if (!t) { t = document.createElement("div"); t.id = "toast"; t.className = "toast"; document.body.appendChild(t); }
    t.textContent = text; t.className = "toast show" + (err ? " err" : "");
    clearTimeout(t._h); t._h = setTimeout(() => t.className = "toast", 4200);
  }
  async function user() {
    const s = await sb(); if (!s) return null;
    const {data} = await s.auth.getSession();
    return data.session ? data.session.user : null;
  }
  // Lance le paiement Stripe (ou change de formule) pour item = essentiel | illimite | pack10 | pack25.
  async function checkout(item, btn) {
    const s = await sb();
    if (!s) return toast("Site non configuré (config.json).", true);
    if (!(await user())) { location.href = "compte.html?plan=" + item; return; }
    if (btn) btn.disabled = true;
    try {
      const {data, error} = await s.functions.invoke("gl-checkout", {body: {item, returnUrl: location.origin + location.pathname.replace(/[^/]*$/, "") + "compte.html"}});
      if (error) {
        let code = error.message;
        try { const j = await error.context.json(); code = j.error || code; } catch {}
        throw new Error(MESSAGES[code] || code);
      }
      if (data.url) { location.href = data.url; return; }
      if (data.updated) { toast("C'est fait ✅"); setTimeout(() => location.href = "compte.html?paiement=ok", 1200); }
    } catch (e) { toast(e.message, true); }
    finally { if (btn) btn.disabled = false; }
  }
  const MESSAGES = {
    already_subscribed: "Tu as déjà cette formule.",
    packs_require_essentiel: "Les packs d'heures sont réservés aux abonnés Essentiel actifs.",
    not_authenticated: "Connecte-toi d'abord.",
    stripe_error: "Le paiement est momentanément indisponible. Réessaie dans un instant.",
  };
  async function header() {
    const slot = document.getElementById("accountBtn");
    if (!slot) return;
    const u = await user().catch(() => null);
    slot.textContent = u ? "Mon compte" : "Connexion";
    slot.href = "compte.html";
  }
  async function footer() {
    const c = await config().catch(() => ({}));
    document.querySelectorAll("[data-support]").forEach((a) => { a.href = "mailto:" + (c.supportEmail || ""); if (!a.textContent.trim()) a.textContent = c.supportEmail || ""; });
    document.querySelectorAll("[data-year]").forEach((n) => n.textContent = new Date().getFullYear());
  }
  document.addEventListener("DOMContentLoaded", () => { header(); footer(); });
  return {config, sb, toast, user, checkout};
})();
