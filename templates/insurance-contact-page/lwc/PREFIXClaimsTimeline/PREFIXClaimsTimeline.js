import { LightningElement, api, wire } from "lwc";
import getClaims from "@salesforce/apex/PREFIXInsuranceProfileController.getClaims";

function fmtCcy(v) {
  const n = Number(v);
  if (!isFinite(n) || n <= 0) return "—";
  if (n >= 1000) return "__CCY_SYMBOL__" + Math.round(n).toLocaleString("__NUM_LOCALE__");
  return "__CCY_SYMBOL__" + n.toFixed(0);
}

export default class PREFIXClaimsTimeline extends LightningElement {
  @api recordId;
  claims = [];
  loading = true;

  @wire(getClaims, { contactId: "$recordId" })
  wired({ data }) {
    this.loading = false;
    if (!data) { this.claims = []; return; }
    this.claims = data.map((c, i) => ({
      id:             c.id || `c-${i}`,
      claimType:      c.claimType   || "General Claim",
      amountFmt:      fmtCcy(c.claimAmount),
      claimDate:      c.claimDate   || "—",
      resolutionDate: c.resolutionDate || null,
      status:         c.status      || "Pending",
      statusClass:    this._statusClass(c.status),
    }));
  }

  _statusClass(s) {
    const st = (s || "").toLowerCase();
    if (st === "approved" || st === "paid") return "status-badge claim-approved";
    if (st === "rejected" || st === "denied") return "status-badge claim-rejected";
    if (st === "pending" || st === "in review") return "status-badge claim-pending";
    return "status-badge claim-other";
  }

  get hasClaims() { return !this.loading && this.claims.length > 0; }
  get showEmpty()  { return !this.loading && this.claims.length === 0; }
}
