import { LightningElement, api, wire } from "lwc";
import getEngagement from "@salesforce/apex/PREFIXInsuranceProfileController.getEngagement";

export default class PREFIXEngagementFeed extends LightningElement {
  @api recordId;
  events = [];
  loading = true;

  @wire(getEngagement, { contactId: "$recordId" })
  wired({ data }) {
    this.loading = false;
    if (!data) { this.events = []; return; }
    this.events = data.map((e, i) => ({
      id:           e.id || `e-${i}`,
      campaignName: e.campaignName  || "Campaign",
      sentDate:     e.sentDate      || "—",
      emailAddress: e.emailAddress  || "",
      opened:       !!e.opened,
      clicked:      !!e.clicked,
      unsubscribed: !!e.unsubscribed,
      notEngaged:   !e.opened && !e.clicked && !e.unsubscribed,
      iconClass:    e.clicked ? "icon icon-clicked" : e.opened ? "icon icon-opened" : "icon icon-sent",
    }));
  }

  get hasEvents() { return !this.loading && this.events.length > 0; }
  get showEmpty()  { return !this.loading && this.events.length === 0; }
}
