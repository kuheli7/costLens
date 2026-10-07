// Data source: the live API when it answers, otherwise the bundled synthetic demo file.
// Money is integer cents everywhere; it is formatted only at the edge, here.

export const usd = (cents) => {
  const neg = cents < 0;
  const c = Math.abs(cents);
  const body = `$${Math.floor(c / 100).toLocaleString("en-US")}.${String(c % 100).padStart(2, "0")}`;
  return neg ? `-${body}` : body;
};

let demoCache = null;

async function demo() {
  if (!demoCache) {
    const res = await fetch(`${import.meta.env.BASE_URL}demo.json`);
    if (!res.ok) throw new Error("demo data missing");
    demoCache = await res.json();
  }
  return demoCache;
}

async function live(path) {
  const res = await fetch(`/api${path}`, { headers: { Accept: "application/json" } });
  if (!res.ok) throw new Error(`${path}: ${res.status}`);
  return res.json();
}

export async function load() {
  try {
    const [trend, waste, explanations] = await Promise.all([
      live("/trend?limit=60"),
      live("/waste"),
      live("/explanations?limit=30"),
    ]);
    return { source: "live", synthetic: false, trend, waste, explanations, details: null };
  } catch {
    const d = await demo();
    return {
      source: "demo",
      synthetic: true,
      trend: d.trend,
      waste: d.waste,
      explanations: d.explanations,
      details: d.details,
    };
  }
}

export async function loadDay(data, day) {
  if (data.details) return data.details[day] ?? null;
  const d = await live(`/days/${day}`);
  return { facts: d.facts, explanation: d.explanation, digest_text: d.digest_text };
}
