import { createClient } from "jsr:@supabase/supabase-js@2";

const STYLE = "Realistic high-end lifestyle travel photography, golden hour late afternoon light, Mediterranean seaside setting with rocky hills, umbrella pines and turquoise water in the background. People shown from behind or three-quarter angle, never close-up on faces. Casual or sporty outfits, warm natural friendly atmosphere among friends, saturated warm colors, shallow depth of field, wide landscape framing. No text, logo or watermark.";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

function hashSeed(s: string): number {
  let h = 0;
  for (let i = 0; i < s.length; i++) {
    h = (Math.imul(31, h) + s.charCodeAt(i)) | 0;
  }
  return Math.abs(h) % 1000000;
}

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

    const prompt = `${STYLE} Scene: ${title}, category ${topcat}${subcat ? "/" + subcat : ""}${lieu ? ", location: " + lieu : ""}. Show people concretely doing this specific activity, recognizable at a glance.`;
    const encoded = encodeURIComponent(prompt);
    const seed = hashSeed(activityId);
    const pollinationsUrl = `https://image.pollinations.ai/prompt/${encoded}?width=1536&height=1024&nologo=true&seed=${seed}`;

    let imgRes: Response;
    try {
      imgRes = await fetch(pollinationsUrl);
    } catch (fetchErr) {
      return new Response(JSON.stringify({ error: "image_fetch_failed: " + String(fetchErr) }), {
        status: 502,
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      });
    }
    if (!imgRes.ok) {
      return new Response(JSON.stringify({ error: `pollinations returned ${imgRes.status}` }), {
        status: 502,
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      });
    }
    const bytes = new Uint8Array(await imgRes.arrayBuffer());
    if (bytes.length < 1000) {
      return new Response(JSON.stringify({ error: "image too small, likely a placeholder/error image" }), {
        status: 502,
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      });
    }

    const supabase = createClient(
      Deno.env.get("SUPABASE_URL")!,
      Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
    );

    const path = `${activityId}-${Date.now()}.jpg`;
    const { error: upErr } = await supabase.storage
      .from("activity-photos")
      .upload(path, bytes, { contentType: "image/jpeg", upsert: true });
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
