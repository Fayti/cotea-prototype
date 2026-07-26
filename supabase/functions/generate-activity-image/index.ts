import { createClient } from "jsr:@supabase/supabase-js@2";

const STYLE = "Photographie realiste de style lifestyle/voyage haut de gamme, lumiere doree de fin d'apres-midi (golden hour), decor de bord de mer mediterraneen avec collines rocheuses, pins parasols et eau turquoise en arriere-plan. Les personnes sont vues de dos ou de trois-quarts, jamais de gros plan sur le visage. Tenues decontractees ou sportives, ambiance chaleureuse, naturelle et conviviale entre amis. Couleurs saturees et chaudes, legere profondeur de champ, cadrage large format paysage. Aucun texte, chiffre, logo ou watermark dans l'image.";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") {
    return new Response("ok", { headers: corsHeaders });
  }
  try {
    const { activityId, title, topcat, subcat, lieu } = await req.json();
    if (!activityId || !title) {
      return new Response(JSON.stringify({ error: "activityId and title are required" }), {
        status: 400,
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      });
    }

    const apiKey = Deno.env.get("OPENAI_API_KEY");
    if (!apiKey) {
      return new Response(JSON.stringify({ error: "OPENAI_API_KEY not configured" }), {
        status: 500,
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      });
    }

    const prompt = `${STYLE}\n\nScene a illustrer : "${title}" (categorie : ${topcat}${subcat ? " / " + subcat : ""}${lieu ? ", lieu : " + lieu : ""}). Montre concretement des personnes en train de pratiquer cette activite precise, de maniere reconnaissable.`;

    const openaiRes = await fetch("https://api.openai.com/v1/images/generations", {
      method: "POST",
      headers: {
        Authorization: `Bearer ${apiKey}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        model: "gpt-image-1",
        prompt,
        size: "1536x1024",
        quality: "medium",
        n: 1,
      }),
    });

    const openaiData = await openaiRes.json();
    if (!openaiRes.ok) {
      return new Response(JSON.stringify({ error: openaiData }), {
        status: 502,
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      });
    }

    const b64 = openaiData.data?.[0]?.b64_json;
    if (!b64) {
      return new Response(JSON.stringify({ error: "no image returned", raw: openaiData }), {
        status: 502,
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      });
    }
    const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));

    const supabase = createClient(
      Deno.env.get("SUPABASE_URL")!,
      Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
    );

    const path = `${activityId}-${Date.now()}.png`;
    const { error: upErr } = await supabase.storage
      .from("activity-photos")
      .upload(path, bytes, { contentType: "image/png", upsert: true });
    if (upErr) {
      return new Response(JSON.stringify({ error: upErr.message }), {
        status: 500,
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      });
    }

    const { data: pub } = supabase.storage.from("activity-photos").getPublicUrl(path);

    const { error: dbErr } = await supabase
      .from("activities")
      .update({ photo: pub.publicUrl })
      .eq("id", activityId);
    if (dbErr) {
      return new Response(JSON.stringify({ error: dbErr.message }), {
        status: 500,
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      });
    }

    return new Response(JSON.stringify({ ok: true, url: pub.publicUrl }), {
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  } catch (e) {
    return new Response(JSON.stringify({ error: String(e) }), {
      status: 500,
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  }
});
