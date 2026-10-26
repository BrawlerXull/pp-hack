// AG Studio in the night console: the same surfaces, ink and validated chart palette as the War Room
// (src/app/globals.css), through Studio's own theming API, so its grids, charts and panels match the app.

import { studioTheme } from "ag-studio";

const NIGHT = {
  page: "#04070e",
  canvas: "#070c18",
  surface: "#0e1628",
  raised: "#121c33",
  border: "rgba(255,255,255,0.08)",
  gridline: "rgba(255,255,255,0.06)",
  ink: "#e7ecf6",
  ink2: "#a9b3c7",
  muted: "#7f8aa3",
  accent: "#4c8dff",
  highlight: "rgba(76,141,255,0.12)",
  font: "var(--font-geist-sans), ui-sans-serif, system-ui, sans-serif",
};

// Studio colours legend entries in alphabetical order of the values present. The first five follow the Result
// groups alphabetically (Declined by bank, Paid, Refunded, Stopped by guard, Unavailable (PayPal error)) in their
// War Room colours; this order is validated for colour-blind separation and contrast on the card surface.
const PALETTE = ["#d95926", "#3987e5", "#c98500", "#199e70", "#9085e9", "#d55181", "#e66767", "#008300"];

const line = (color: string) => ({ width: 1, color });

const palette = Object.fromEntries(
  PALETTE.flatMap((c, i) => [
    [`chartPaletteFills${i + 1}Color`, c],
    [`chartPaletteStrokes${i + 1}Color`, c],
  ]),
);

export const nightStudioTheme = studioTheme.withParams({
  fontFamily: NIGHT.font,
  fontSize: 13,
  textColor: NIGHT.ink,
  foregroundColor: NIGHT.ink,
  subtleTextColor: NIGHT.muted,
  backgroundColor: NIGHT.surface,
  borderColor: NIGHT.border,
  accentColor: NIGHT.accent,
  iconColor: NIGHT.ink2,
  rowHoverColor: NIGHT.highlight,

  studioWrapperBorder: false,
  studioWrapperBorderRadius: 0,
  studioWrapperSpacing: 0,
  studioWrapperBackgroundColor: NIGHT.canvas,
  studioCanvasBorder: false,
  studioCanvasBorderRadius: 0,
  studioCanvasShadow: false,
  studioCanvasBackgroundColor: NIGHT.canvas,
  studioCanvasPadding: 16,
  studioCanvasRowHeight: 18,
  studioCanvasFontFamily: NIGHT.font,

  studioWidgetBackgroundColor: NIGHT.surface,
  studioWidgetBorder: line(NIGHT.border),
  studioWidgetBorderRadius: 14,
  studioWidgetPadding: 12,
  studioWidgetTitleFontFamily: NIGHT.font,
  studioWidgetTitleFontSize: 14,
  studioWidgetTitleFontWeight: 600,
  studioWidgetTitleTextColor: NIGHT.ink,
  studioWidgetSubtitleFontFamily: NIGHT.font,
  studioWidgetSubtitleFontSize: 12,
  studioWidgetSubtitleTextColor: NIGHT.muted,
  studioWidgetCaptionFontFamily: NIGHT.font,
  studioWidgetCaptionFontSize: 12,
  studioWidgetCaptionTextColor: NIGHT.muted,
  studioWidgetResizeBorderColor: NIGHT.accent,

  studioPanelContainerBackgroundColor: NIGHT.raised,
  studioPanelContainerBorder: line(NIGHT.border),
  studioPanelContainerBorderRadius: 0,
  studioPanelDividerColor: NIGHT.border,
  studioPanelDividerActiveColor: NIGHT.accent,
  studioPanelSectionBorderColor: NIGHT.border,
  studioPanelHeaderFontFamily: NIGHT.font,
  studioPanelHeaderFontWeight: 600,
  studioPanelChevronButtonHoverBackgroundColor: NIGHT.highlight,
  studioPanelChevronButtonHoverColor: NIGHT.accent,
  studioToggleButtonActiveBackgroundColor: NIGHT.highlight,
  studioToggleButtonActiveBorderColor: NIGHT.accent,
  studioToggleButtonActiveColor: NIGHT.accent,

  gridFontFamily: NIGHT.font,
  gridFontSize: 13,
  gridCellTextColor: NIGHT.ink,
  gridHeaderBackgroundColor: NIGHT.raised,
  gridHeaderTextColor: NIGHT.ink2,
  gridHeaderFontWeight: 500,
  gridHeaderFontSize: 12,
  gridRowHeight: 36,
  gridRowBorder: line(NIGHT.gridline),
  gridHeaderRowBorder: line(NIGHT.border),
  gridPinnedRowBorder: line(NIGHT.border),
  gridRowHoverColor: NIGHT.highlight,
  gridWrapperBorder: false,

  chartFontFamily: NIGHT.font,
  chartTextColor: NIGHT.ink2,
  chartSubtleTextColor: NIGHT.muted,
  chartAxisLineColor: NIGHT.border,
  chartGridLineColor: NIGHT.gridline,
  ...palette,
});
