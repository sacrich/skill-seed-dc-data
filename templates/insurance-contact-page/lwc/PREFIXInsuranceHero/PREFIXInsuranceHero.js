import { LightningElement, api, wire } from "lwc";
import { getRecord, getFieldValue } from "lightning/uiRecordApi";
import getInsuranceBundle from "@salesforce/apex/PREFIXInsuranceProfileController.getInsuranceBundle";

const FIELDS = [
  "Contact.FirstName", "Contact.LastName", "Contact.Email", "Contact.Phone",
  "Contact.MailingCity", "Contact.MailingCountry"
];

// SVG gauge arc: the background semicircle has a circumference of ~157px (half of π×50×2).
// We use stroke-dasharray/dashoffset to fill proportionally to the churn score.
const GAUGE_CIRCUMFERENCE = 157.08; // π × r(50) = half circle

function fmtCcy(v) {
  const n = Number(v);
  if (!isFinite(n) || n <= 0) return "—";
  // Compact form: ≥1M → "X.XM", ≥1K → "Xk"
  if (n >= 1_000_000) return "__CCY_SYMBOL__" + (n / 1_000_000).toFixed(1) + "M";
  if (n >= 1000)      return "__CCY_SYMBOL__" + Math.round(n / 1000) + "k";
  return "__CCY_SYMBOL__" + Math.round(n).toLocaleString("__NUM_LOCALE__");
}

export default class PREFIXInsuranceHero extends LightningElement {
  @api recordId;

  @wire(getRecord, { recordId: "$recordId", fields: FIELDS }) record;

  bundle = null;

  @wire(getInsuranceBundle, { contactId: "$recordId" })
  wiredBundle({ data }) {
    if (data) this.bundle = data;
  }

  // ── Profile fields ──────────────────────────────────────────────
  get firstName() { return this.bundle?.firstName || getFieldValue(this.record.data, "Contact.FirstName") || ""; }
  get lastName()  { return this.bundle?.lastName  || getFieldValue(this.record.data, "Contact.LastName")  || ""; }
  get fullName()  { return `${this.firstName} ${this.lastName}`.trim() || "—"; }
  get email()     { return this.bundle?.email  || getFieldValue(this.record.data, "Contact.Email") || "—"; }
  get phone()     { return this.bundle?.phone  || getFieldValue(this.record.data, "Contact.Phone") || "—"; }
  get city()      { return this.bundle?.city   || getFieldValue(this.record.data, "Contact.MailingCity") || ""; }
  get country()   { return this.bundle?.country|| getFieldValue(this.record.data, "Contact.MailingCountry") || ""; }
  get cityCountry() { return [this.city, this.country].filter(Boolean).join(", ") || "—"; }

  get initials() {
    const a = (this.firstName || " ").trim()[0] || "?";
    const b = (this.lastName  || " ").trim()[0] || "";
    return (a + b).toUpperCase();
  }

  get isVip() {
    const t = (this.bundle?.loyaltyTier || "").toUpperCase();
    return ["VIP", "PLATINUM", "ELITE", "GOLD", "PREMIER", "DIAMOND"].includes(t);
  }

  // ── Churn risk gauge ────────────────────────────────────────────
  get churnScore() { return this.bundle?.churnScore ?? 0; }

  get churnPct() {
    const s = Number(this.churnScore);
    return isFinite(s) ? Math.round(s) + "%" : "—";
  }

  get gaugeStyle() {
    // Fill the arc proportionally: dasharray = full circumference, dashoffset = empty portion.
    const s = Math.max(0, Math.min(100, Number(this.churnScore) || 0));
    const filled = (s / 100) * GAUGE_CIRCUMFERENCE;
    const empty  = GAUGE_CIRCUMFERENCE - filled;
    return `stroke-dasharray:${filled} ${empty};stroke:${this._gaugeColor(s)}`;
  }

  _gaugeColor(score) {
    if (score >= 70) return "#ef4444";   // red  — high risk
    if (score >= 40) return "#f59e0b";   // amber — medium
    return "#22c55e";                     // green — low
  }

  get churnRiskLabel() {
    const s = Number(this.churnScore) || 0;
    if (s >= 70) return "High Risk";
    if (s >= 40) return "Medium Risk";
    return "Low Risk";
  }

  get churnRiskClass() {
    const s = Number(this.churnScore) || 0;
    if (s >= 70) return "risk-tag risk-high";
    if (s >= 40) return "risk-tag risk-mid";
    return "risk-tag risk-low";
  }

  // ── Insurance KPIs ──────────────────────────────────────────────
  get activePolicies()     { return Math.round(this.bundle?.activePolicies  || 0); }
  get claimsThisYear()     { return Math.round(this.bundle?.claimsThisYear  || 0); }
  get monthlyPremiumFmt()  { return fmtCcy(this.bundle?.totalPremium); }
  get coverageFmt()        { return fmtCcy(this.bundle?.totalCoverage); }

  get thisYear() { return new Date().getFullYear(); }

  // ── LTV + NPS ───────────────────────────────────────────────────
  get hasLtv()  { const v = this.bundle?.ltv;      return v != null && Number(v) > 0; }
  get hasNps()  { const v = this.bundle?.npsScore;  return v != null && isFinite(Number(v)); }
  get ltvFmt()  { return fmtCcy(this.bundle?.ltv); }
  get npsFmt()  {
    const v = this.bundle?.npsScore;
    return v != null ? Math.round(Number(v) * 10) / 10 + " / 10" : "—";
  }

  // ── Loyalty tier badge ──────────────────────────────────────────
  get hasLoyalty()  { return !!this.bundle?.loyaltyTier; }
  get loyaltyClass() {
    const t = (this.bundle?.loyaltyTier || "").toLowerCase();
    return `badge-card loyalty loyalty-${t || "none"}`;
  }
}
