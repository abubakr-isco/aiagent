import { useState } from "react";

import { post, type PositionRow } from "../api";
import { useConsole } from "../components/Shell";
import { Confirm, Empty, Section, Side } from "../components/ui";
import { price, signed, time, tone } from "../format";

export function PositionsTable({ positions, currency }: { positions: PositionRow[]; currency: string }) {
  if (!positions.length) return <Empty>No open positions on the account.</Empty>;
  return (
    <div className="scroll-x">
      <table className="table">
        <thead>
          <tr>
            <th>Ticket</th><th>Symbol</th><th>Side</th><th className="num">Lots</th><th className="num">Open</th><th className="num">Current</th>
            <th className="num">Stop loss</th><th className="num">Take profit</th><th className="num">P&amp;L {currency}</th><th>Owner</th><th>Opened</th>
          </tr>
        </thead>
        <tbody>
          {positions.map((p) => (
            <tr key={p.ticket}>
              <td>{p.ticket}</td><td>{p.symbol}</td><td><Side side={p.side} /></td><td className="num">{p.volume}</td>
              <td className="num">{price(p.price_open)}</td><td className="num">{price(p.price_current)}</td>
              <td className={`num ${p.sl ? "" : "neg"}`}>{p.sl ? price(p.sl) : "none"}</td><td className="num">{price(p.tp)}</td>
              <td className={`num ${tone(p.profit + p.swap)}`}>{signed(p.profit + p.swap)}</td>
              <td>{p.agent ? "Agent" : "Manual"}</td><td>{time(p.time)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function Positions() {
  const { status, can, reloadStatus } = useConsole();
  const [confirming, setConfirming] = useState(false);
  if (!status) return null;
  const agentCount = status.positions.filter((p) => p.agent).length;

  return (
    <>
      <Section
        title="Open positions"
        aside={can("admin") && agentCount > 0 ? <button className="btn btn-danger-outline" onClick={() => setConfirming(true)}>Close agent positions</button> : null}
      >
        <p className="muted">Read from the terminal every cycle. Manual positions are never touched by the agent, but they count toward the risk limits.</p>
        <PositionsTable positions={status.positions} currency={status.account?.currency ?? ""} />
      </Section>
      <Section title="Pending orders">
        {status.orders.length === 0 ? <Empty>No pending orders.</Empty> : (
          <table className="table">
            <thead><tr><th>Ticket</th><th>Symbol</th><th className="num">Lots</th><th className="num">Price</th><th>Owner</th></tr></thead>
            <tbody>
              {status.orders.map((o) => (
                <tr key={o.ticket}><td>{o.ticket}</td><td>{o.symbol}</td><td className="num">{o.volume}</td><td className="num">{price(o.price)}</td><td>{o.agent ? "Agent" : "Manual"}</td></tr>
              ))}
            </tbody>
          </table>
        )}
      </Section>
      {confirming && (
        <Confirm
          danger
          title={`Close ${agentCount} agent position${agentCount > 1 ? "s" : ""} at market?`}
          body={<p>Closing now realises the current profit or loss, including any loss. Manual positions are not affected.</p>}
          phrase="CLOSE AGENT POSITIONS"
          confirmLabel="Close positions"
          onConfirm={async ({ phrase }) => {
            await post("/control/command/", { command: "close_agent_positions", confirm: phrase });
            reloadStatus();
          }}
          onClose={() => setConfirming(false)}
        />
      )}
    </>
  );
}
