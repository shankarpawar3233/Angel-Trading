import { Callout, Divider, Grid, H1, H2, Stack, Stat, Table, Text } from "cursor/canvas";

export default function OSIMarketAnalysis20260429() {
  const counts = {
    historyToday: 2,
    rejectedToday: 0,
    paperRowsToday: 106,
    paperClosed: 16,
    activePositions: 1,
  };

  const pnl = {
    realized: -1091480,
    openMtm: -585,
    total: -1092065,
  };

  const topLosers = [
    ["09:45", "NIFTY_20260428_24100_PE", "46.50", "198.35", "1300", "-197405"],
    ["11:58", "NIFTY_20260505_24250_CE", "227.15", "162.00", "1300", "-84695"],
    ["11:53", "NIFTY_20260505_24250_PE", "164.35", "227.15", "1300", "-81640"],
    ["11:19", "NIFTY_20260505_24250_CE", "226.95", "164.25", "1300", "-81510"],
    ["10:48", "NIFTY_20260505_24250_PE", "173.45", "226.95", "1300", "-69550"],
  ];

  const activePosition = [
    ["NIFTY", "BUY_PE", "intrabar_engine", "NIFTY_20260505_24300_PE", "181.70", "182.15", "1300", "-585"],
  ];

  return (
    <Stack gap={16}>
      <H1>OSI Market Analysis - 29 Apr 2026</H1>
      <Text tone="secondary">Scope: today (IST market day). Source: local signal storage.</Text>

      <Grid columns={4} gap={12}>
        <Stat label="Paper Total PnL" value={pnl.total.toFixed(2)} tone="error" />
        <Stat label="Realized PnL" value={pnl.realized.toFixed(2)} tone="error" />
        <Stat label="Open MTM" value={pnl.openMtm.toFixed(2)} tone="warning" />
        <Stat label="Closed Paper Trades" value={String(counts.paperClosed)} />
      </Grid>

      <Grid columns={4} gap={12}>
        <Stat label="History Signals" value={String(counts.historyToday)} />
        <Stat label="Rejected Signals" value={String(counts.rejectedToday)} />
        <Stat label="Paper Rows Logged" value={String(counts.paperRowsToday)} />
        <Stat label="Active Positions" value={String(counts.activePositions)} />
      </Grid>

      <Callout tone="warning" title="Primary finding">
        Today all closed paper exits are from `intrabar_engine` and all 16 are losses. No winning closed trade recorded.
      </Callout>

      <Divider />

      <H2>Worst Closed Trades (Top 5)</H2>
      <Table
        headers={["Time IST", "Option", "Entry", "Exit/LTP", "Qty", "PnL"]}
        rows={topLosers}
        rowTone={["error", "error", "error", "error", "error"]}
      />

      <H2>Current Active Paper Position</H2>
      <Table
        headers={["Symbol", "Signal", "Engine", "Option", "Entry", "LTP", "Qty", "PnL"]}
        rows={activePosition}
        rowTone={["warning"]}
      />

      <Divider />

      <H2>Interpretation</H2>
      <Text>- Trade sizing is very large (`qty` around 1300), which amplifies every adverse move.</Text>
      <Text>- There are almost no accepted non-intrabar outcomes in today paper log, so diversification is effectively absent.</Text>
      <Text>- Rejected signals are zero in this slice, meaning losses are not from entry filters blocking; they are from entry/exit quality and sizing.</Text>
    </Stack>
  );
}
