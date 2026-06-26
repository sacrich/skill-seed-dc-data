import { LightningElement, api, wire } from "lwc";
import getPolicies from "@salesforce/apex/PREFIXInsuranceProfileController.getPolicies";

function fmtCcy(v) {
  const n = Number(v);
  if (!isFinite(n) || n <= 0) return "—";
  if (n >= 1_000_000) return "__CCY_SYMBOL__" + (n / 1_000_000).toFixed(1) + "M";
  if (n >= 1000)      return "__CCY_SYMBOL__" + Math.round(n / 1000) + "k";
  return "__CCY_SYMBOL__" + Math.round(n).toLocaleString("__NUM_LOCALE__");
}

export default class PREFIXPolicyList extends LightningElement {
  @api recordId;
  policies = [];
  loading = true;

  @wire(getPolicies, { contactId: "$recordId" })
  wired({ data }) {
    this.loading = false;
    if (!data) { this.policies = []; return; }
    this.policies = data.map((p, i) => ({
      id:              p.id || `p-${i}`,
      policyNumber:    p.policyNumber || "—",
      productName:     p.productName  || "—",
      productCategory: p.productCategory || "—",
      premiumFmt:      fmtCcy(p.premiumMonthly),
      coverageFmt:     fmtCcy(p.coverageAmount),
      startDate:       p.startDate || "—",
      status:          p.status || "Unknown",
      statusClass:     this._statusClass(p.status),
    }));
  }

  _statusClass(s) {
    const st = (s || "").toLowerCase();
    if (st === "active")  return "status-badge status-active";
    if (st === "lapsed")  return "status-badge status-lapsed";
    if (st === "pending") return "status-badge status-pending";
    return "status-badge status-other";
  }

  get hasPolicies() { return !this.loading && this.policies.length > 0; }
  get showEmpty()   { return !this.loading && this.policies.length === 0; }
}
