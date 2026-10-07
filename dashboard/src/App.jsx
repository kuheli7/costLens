import React, { useEffect, useMemo, useState } from "react";
import {
  CartesianGrid,
  Line,
  LineChart,
  ReferenceDot,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { load, loadDay, usd } from "./api.js";

const shortDay = (iso) =>
  new Date(`${iso}T00:00:00Z`).toLocaleDateString("en-GB", {
    day: "numeric",
    month: "short",
    timeZone: "UTC",
  });

function TrendTooltip({ active, payload }) {
  if (!active || !payload?.length) return null;
  const p = payload[0].payload;
  const delta = p.total_cents - p.baseline_cents;
  return (
    <div className="tip">
      <strong>{shortDay(p.day)}</strong>
      <div>Spend {usd(p.total_cents)}</div>
      <div>Usual {usd(p.baseline_cents)}</div>
      <div>
        Change {delta >= 0 ? "+" : ""}
        {usd(delta)}
      </div>
      {p.significant && <div className="flag">Flagged as a significant change</div>}
    </div>
  );
}

function Trend({ trend, selected, onSelect, asTable }) {
  const flagged = trend.filter((t) => t.significant);
  if (asTable) {
    return (
      <table className="grid">
        <thead>
          <tr>
            <th>Day</th>
            <th className="num">Spend</th>
            <th className="num">Usual</th>
            <th className="num">Change</th>
            <th>Flag</th>
          </tr>
        </thead>
        <tbody>
          {[...trend].reverse().map((t) => (
            <tr key={t.day} onClick={() => onSelect(t.day)} className={t.day === selected ? "sel" : ""}>
              <td>{shortDay(t.day)}</td>
              <td className="num">{usd(t.total_cents)}</td>
              <td className="num">{usd(t.baseline_cents)}</td>
              <td className="num">{usd(t.total_cents - t.baseline_cents)}</td>
              <td>{t.significant ? "Significant change" : ""}</td>
            </tr>
          ))}
        </tbody>
      </table>
    );
  }
  return (
    <div className="chart" role="img" aria-label="Daily AWS spend against the usual level">
      <ResponsiveContainer width="100%" height={280}>
        <LineChart
          data={trend}
          margin={{ top: 12, right: 16, bottom: 4, left: 4 }}
          onClick={(e) => e?.activeLabel && onSelect(e.activeLabel)}
        >
          <CartesianGrid stroke="var(--grid)" vertical={false} />
          <XAxis
            dataKey="day"
            tickFormatter={shortDay}
            stroke="var(--axis)"
            tick={{ fill: "var(--muted)", fontSize: 12 }}
            tickLine={false}
            minTickGap={28}
          />
          <YAxis
            tickFormatter={(c) => `$${Math.round(c / 100)}`}
            stroke="var(--axis)"
            tick={{ fill: "var(--muted)", fontSize: 12 }}
            tickLine={false}
            axisLine={false}
            width={48}
          />
          <Tooltip content={<TrendTooltip />} cursor={{ stroke: "var(--axis)", strokeWidth: 1 }} />
          <Line
            type="monotone"
            dataKey="baseline_cents"
            name="Usual"
            stroke="var(--muted)"
            strokeWidth={2}
            strokeDasharray="5 4"
            dot={false}
            isAnimationActive={false}
          />
          <Line
            type="monotone"
            dataKey="total_cents"
            name="Spend"
            stroke="var(--series-1)"
            strokeWidth={2}
            dot={false}
            activeDot={{ r: 5, stroke: "var(--surface)", strokeWidth: 2 }}
            isAnimationActive={false}
          />
          {flagged.map((t) => (
            <ReferenceDot
              key={t.day}
              x={t.day}
              y={t.total_cents}
              r={5}
              fill="var(--critical)"
              stroke="var(--surface)"
              strokeWidth={2}
              ifOverflow="visible"
            />
          ))}
        </LineChart>
      </ResponsiveContainer>
      <ul className="legend">
        <li>
          <span className="swatch solid" /> Spend
        </li>
        <li>
          <span className="swatch dashed" /> Usual (same-weekday median)
        </li>
        <li>
          <span className="swatch dot" /> Flagged day
        </li>
      </ul>
    </div>
  );
}

function DayDetail({ detail }) {
  if (!detail) return <p className="muted">Select a day on the chart to see why it changed.</p>;
  const { facts, explanation } = detail;
  const byId = Object.fromEntries(facts.causes.map((c) => [c.id, c]));
  return (
    <div>
      <h3>{facts.day}</h3>
      <p className="headline">{explanation.headline}</p>
      <p className="muted">
        {facts.total_spend} against a usual {facts.typical_spend}
        {facts.significant_change ? ` (${facts.change}, ${facts.change_pct})` : ""}
      </p>
      {explanation.causes.map((c, i) => {
        const fact = byId[c.id];
        return (
          <div className="cause" key={c.id}>
            <div className="cause-amount">
              {i + 1}. {fact.change}
            </div>
            <div>{c.explanation}</div>
            <div className="muted">Fix: {c.fix}</div>
          </div>
        );
      })}
      <p className="muted small">{explanation.confidence_note}</p>
      <p className="small">
        Text written by the <strong>{explanation.source}</strong> writer. Every figure is checked
        against fact sheet <code>{explanation.facts_hash}</code>.
      </p>
    </div>
  );
}

function Waste({ waste }) {
  const { findings, savings } = waste;
  return (
    <div>
      <p className="muted">
        Open: <strong>{usd(savings.open_cents)}</strong>/month. Saved (priced from your bill):{" "}
        <strong>{usd(savings.saved_cents)}</strong>/month.
      </p>
      {findings.length === 0 ? (
        <p className="muted">No waste findings.</p>
      ) : (
        <table className="grid">
          <thead>
            <tr>
              <th>Resource</th>
              <th>Finding</th>
              <th className="num">Saves / month</th>
              <th>Basis</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {findings.map((f) => (
              <tr key={`${f.rule}/${f.resource_id}`}>
                <td>
                  <code>{f.resource_id}</code>
                </td>
                <td>
                  {f.summary}
                  <div className="muted small">
                    <code>{f.fix}</code>
                  </div>
                </td>
                <td className="num">{usd(f.monthly_savings_cents)}</td>
                <td>{f.savings_source === "cur" ? "From your bill" : "Rough estimate"}</td>
                <td>{f.status}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function History({ items, onSelect }) {
  return (
    <ul className="history">
      {items.map((e) => (
        <li key={`${e.day}-${e.facts_hash}`}>
          <button type="button" onClick={() => onSelect(e.day)}>
            <span className="muted">{shortDay(e.day)}</span> {e.headline}
          </button>
        </li>
      ))}
    </ul>
  );
}

export default function App() {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [selected, setSelected] = useState(null);
  const [detail, setDetail] = useState(null);
  const [asTable, setAsTable] = useState(false);

  useEffect(() => {
    load()
      .then((d) => {
        setData(d);
        const flagged = [...d.trend].reverse().find((t) => t.significant);
        setSelected(flagged?.day ?? d.trend.at(-1)?.day ?? null);
      })
      .catch((e) => setError(String(e)));
  }, []);

  useEffect(() => {
    if (data && selected) loadDay(data, selected).then(setDetail).catch(() => setDetail(null));
  }, [data, selected]);

  const summary = useMemo(() => {
    if (!data) return null;
    const last = data.trend.at(-1);
    return last && { ...last, delta: last.total_cents - last.baseline_cents };
  }, [data]);

  if (error) return <main className="page"><p role="alert">Could not load data: {error}</p></main>;
  if (!data) return <main className="page"><p className="muted">Loading</p></main>;

  return (
    <main className="page">
      <header>
        <h1>CostLens</h1>
        <p className="muted">
          Why your AWS bill changed, and what to do about it.
          {data.synthetic && <span className="badge">Demo: synthetic data</span>}
        </p>
      </header>

      {summary && (
        <section className="stats" aria-label="Latest day">
          <div>
            <div className="stat-label">Latest day ({shortDay(summary.day)})</div>
            <div className="stat-value">{usd(summary.total_cents)}</div>
          </div>
          <div>
            <div className="stat-label">Against usual</div>
            <div className="stat-value">
              {summary.delta >= 0 ? "+" : ""}
              {usd(summary.delta)}
            </div>
          </div>
          <div>
            <div className="stat-label">Open waste</div>
            <div className="stat-value">{usd(data.waste.savings.open_cents)}/mo</div>
          </div>
        </section>
      )}

      <section>
        <div className="row">
          <h2>Daily spend</h2>
          <button type="button" className="link" onClick={() => setAsTable((v) => !v)}>
            {asTable ? "Show chart" : "Show table"}
          </button>
        </div>
        <Trend trend={data.trend} selected={selected} onSelect={setSelected} asTable={asTable} />
      </section>

      <section>
        <h2>Why it changed</h2>
        <DayDetail detail={detail} />
      </section>

      <section>
        <h2>Waste</h2>
        <Waste waste={data.waste} />
      </section>

      <section>
        <h2>Explanation history</h2>
        <History items={data.explanations} onSelect={setSelected} />
      </section>
    </main>
  );
}
