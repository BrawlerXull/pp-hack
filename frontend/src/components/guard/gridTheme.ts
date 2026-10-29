import { themeQuartz } from "ag-grid-community";

/**
 * The one AG Grid theme for the dashboard: Quartz in the app's light tokens (white rows, grey header, hairline
 * rows, the blue accent for focus and selection). Grids sit flush inside a Card (bodyClassName="p-0"), so the
 * grid itself draws no outer border.
 */
export const gridTheme = themeQuartz.withParams({
  backgroundColor: "#ffffff",
  foregroundColor: "#101828",
  textColor: "#101828",
  subtleTextColor: "#5d6b82",
  chromeBackgroundColor: "#f9fafb",
  headerBackgroundColor: "#f9fafb",
  headerTextColor: "#344054",
  headerFontSize: 13,
  headerFontWeight: 600,
  borderColor: "#e4e7ec",
  rowBorder: { color: "#eef0f3" },
  columnBorder: false,
  wrapperBorder: false,
  wrapperBorderRadius: 0,
  rowHoverColor: "#f5f8ff",
  selectedRowBackgroundColor: "#eaf2fe",
  accentColor: "#0070e0",
  fontFamily: "Geist, 'Geist Fallback', ui-sans-serif, system-ui, sans-serif",
  fontSize: 13,
  rowHeight: 44,
  headerHeight: 42,
  cellHorizontalPadding: 16,
  browserColorScheme: "light",
});
