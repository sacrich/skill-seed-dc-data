public with sharing class PREFIXInsuranceProfileController {
    /*
     * __CLIENT__ Insurance Customer 360 — Data Cloud profile controller.
     *
     * Queries Data Cloud via runSql() → POST /services/data/v62.0/ssot/query.
     * Auth: JWT-Bearer flow (same key the data-seeding scripts use).
     *
     * DMOs queried:
     *   ssot__Individual__dlm / UnifiedIndividual__dlm — profile
     *   IndividualIdentityLink__dlm               — identity resolution
     *   InsurancePolicy__dlm                      — policies
     *   InsuranceClaim__dlm                       — claims
     *   EmailEngagement__dlm                      — email engagement
     *
     * Build-time tokens (substituted by deploy_insurance_page.py):
     *   __JWT_CONSUMER_KEY__    Connected App consumer key
     *   __ADMIN_USERNAME__      Org admin username (JWT sub)
     *   __JWT_PKCS8_DER_BASE64_ PKCS#8 DER private key base64
     *   __CONTACTS_DLO__        DLO API name of the contacts stream
     *                           (e.g. MigdalContacts — slug-derived)
     *   __C_PRIMARY__           Brand colour tokens (same as LWC CSS)
     *   __CCY_SYMBOL__          Currency symbol (₪ / $ / € …)
     */

    /* =========================================================== */
    /* CDP AUTH — in-Apex JWT Bearer (no Named Credential)          */
    /* =========================================================== */
    private static final String JWT_CONSUMER_KEY = '__JWT_CONSUMER_KEY__';
    private static final String JWT_SUBJECT      = '__ADMIN_USERNAME__';
    private static final String JWT_AUDIENCE     = 'https://login.salesforce.com';
    private static final String JWT_KEY_B64      = '__JWT_PKCS8_DER_BASE64__';

    private static String CACHED_CORE_TOKEN;
    private static String CACHED_CDP_TOKEN;
    private static String CACHED_CDP_TENANT;
    private static Boolean AUTH_ATTEMPTED = false;

    private static void ensureCdpAuth() {
        if (AUTH_ATTEMPTED) return;
        AUTH_ATTEMPTED = true;
        try {
            String header = base64Url(Blob.valueOf('{"alg":"RS256","typ":"JWT"}'));
            Long exp = (DateTime.now().getTime() / 1000) + 240;
            String claim = base64Url(Blob.valueOf(
                '{"iss":"' + JWT_CONSUMER_KEY + '","sub":"' + JWT_SUBJECT +
                '","aud":"' + JWT_AUDIENCE + '","exp":' + exp + '}'));
            String signingInput = header + '.' + claim;
            Blob sig = Crypto.sign('RSA-SHA256', Blob.valueOf(signingInput),
                                   EncodingUtil.base64Decode(JWT_KEY_B64));
            String assertion = signingInput + '.' + base64Url(sig);

            String orgUrl = URL.getOrgDomainUrl().toExternalForm();
            HttpRequest r1 = new HttpRequest();
            r1.setTimeout(30000);
            r1.setEndpoint(orgUrl + '/services/oauth2/token');
            r1.setMethod('POST');
            r1.setHeader('Content-Type', 'application/x-www-form-urlencoded');
            r1.setBody('grant_type=urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Ajwt-bearer' +
                       '&assertion=' + EncodingUtil.urlEncode(assertion, 'UTF-8'));
            HttpResponse p1 = new Http().send(r1);
            if (p1.getStatusCode() != 200) return;
            Map<String, Object> core = (Map<String, Object>) JSON.deserializeUntyped(p1.getBody());
            CACHED_CORE_TOKEN = (String) core.get('access_token');

            HttpRequest r2 = new HttpRequest();
            r2.setTimeout(30000);
            r2.setEndpoint(orgUrl + '/services/a360/token');
            r2.setMethod('POST');
            r2.setHeader('Content-Type', 'application/x-www-form-urlencoded');
            r2.setBody('grant_type=urn%3Asalesforce%3Agrant-type%3Aexternal%3Acdp' +
                       '&subject_token=' + EncodingUtil.urlEncode(CACHED_CORE_TOKEN, 'UTF-8') +
                       '&subject_token_type=urn%3Aietf%3Aparams%3Aoauth%3Atoken-type%3Aaccess_token');
            HttpResponse p2 = new Http().send(r2);
            if (p2.getStatusCode() == 200) {
                Map<String, Object> cdp = (Map<String, Object>) JSON.deserializeUntyped(p2.getBody());
                CACHED_CDP_TOKEN = (String) cdp.get('access_token');
                String tenant = (String) cdp.get('instance_url');
                if (tenant != null) CACHED_CDP_TENANT = tenant.startsWith('http') ? tenant : 'https://' + tenant;
            }
        } catch (Exception ex) { System.debug('ensureCdpAuth: ' + ex.getMessage()); }
    }

    private static String base64Url(Blob b) {
        return EncodingUtil.base64Encode(b).replace('+', '-').replace('/', '_').replace('=', '');
    }

    /* =========================================================== */
    /* Insurance Bundle — hero + KPIs in one call                   */
    /* =========================================================== */
    @AuraEnabled(cacheable=true)
    public static InsuranceBundle getInsuranceBundle(Id contactId) {
        InsuranceBundle b = new InsuranceBundle();
        try {
            Contact c = [SELECT FirstName, LastName, Email, Phone,
                                MailingStreet, MailingCity, MailingCountry, MailingPostalCode
                         FROM Contact WHERE Id = :contactId LIMIT 1];
            b.firstName = c.FirstName;
            b.lastName  = c.LastName;
            b.email     = c.Email;
            b.phone     = c.Phone;
            b.city      = c.MailingCity;
            b.country   = c.MailingCountry;

            if (String.isBlank(c.Email)) return b;

            String unifiedId = resolveUnifiedId(contactId, c.Email);
            if (String.isBlank(unifiedId)) return b;
            b.unifiedId = unifiedId;
            String safeUid = unifiedId.replace('\'', '\\\'');

            // Profile fields from UnifiedIndividual (standard + seeded custom fields)
            List<Map<String, Object>> ui = runSql(
                'SELECT ssot__FirstName__c, ssot__LastName__c, ssot__ResidenceCountryName__c ' +
                'FROM UnifiedIndividual__dlm WHERE ssot__Id__c = \'' + safeUid + '\' LIMIT 1');
            if (ui != null && !ui.isEmpty()) {
                String uFirst = sv(ui[0], 'ssot__FirstName__c');
                String uLast  = sv(ui[0], 'ssot__LastName__c');
                if (!String.isBlank(uFirst)) b.firstName = uFirst;
                if (!String.isBlank(uLast))  b.lastName  = uLast;
            }

            // Churn score + LTV + NPS from the contacts source DLO (seeded fields)
            // __CONTACTS_DLO__ is the DLO API name, injected at deploy time.
            final String DLO = '__CONTACTS_DLO__';
            if (!DLO.startsWith('__')) {
                List<String> srcIds = resolveSourceIds(unifiedId);
                if (!srcIds.isEmpty()) {
                    String inList = '';
                    for (String s : srcIds) inList += (inList.length() == 0 ? '' : ',') + '\'' + s.replace('\'', '\\\'') + '\'';
                    List<Map<String, Object>> demo = runSql(
                        'SELECT churn_score, ltv, nps_score, loyalty_tier ' +
                        'FROM ' + DLO + ' WHERE id IN (' + inList + ') LIMIT 1');
                    if (demo != null && !demo.isEmpty()) {
                        b.churnScore   = nv(demo[0], 'churn_score');
                        b.ltv          = nv(demo[0], 'ltv');
                        b.npsScore     = nv(demo[0], 'nps_score');
                        b.loyaltyTier  = sv(demo[0], 'loyalty_tier');
                    }
                }
            }

            // Policy summary KPIs — aggregate per individual
            List<Map<String, Object>> pkpi = runSql(
                'SELECT COUNT(Id__c) policy_count, SUM(PremiumMonthly__c) total_premium, ' +
                'SUM(CoverageAmount__c) total_coverage ' +
                'FROM InsurancePolicy__dlm ' +
                'WHERE PartyId__c = \'' + safeUid + '\' AND Status__c = \'Active\'');
            if (pkpi != null && !pkpi.isEmpty()) {
                b.activePolicies   = nv(pkpi[0], 'policy_count');
                b.totalPremium     = nv(pkpi[0], 'total_premium');
                b.totalCoverage    = nv(pkpi[0], 'total_coverage');
            }

            // All policies count (including non-active)
            List<Map<String, Object>> allp = runSql(
                'SELECT COUNT(Id__c) all_count FROM InsurancePolicy__dlm WHERE PartyId__c = \'' + safeUid + '\'');
            if (allp != null && !allp.isEmpty()) b.totalPolicies = nv(allp[0], 'all_count');

            // Claims count this year
            String thisYear = String.valueOf(Date.today().year());
            List<Map<String, Object>> clms = runSql(
                'SELECT COUNT(Id__c) claim_count, SUM(ClaimAmount__c) claim_total ' +
                'FROM InsuranceClaim__dlm ' +
                'WHERE PartyId__c = \'' + safeUid + '\'' +
                ' AND ClaimDate__c >= \'' + thisYear + '-01-01\'');
            if (clms != null && !clms.isEmpty()) {
                b.claimsThisYear   = nv(clms[0], 'claim_count');
                b.claimAmountTotal = nv(clms[0], 'claim_total');
            }

        } catch (Exception ex) { b.error = ex.getMessage(); }
        return b;
    }

    /* =========================================================== */
    /* Policies — list for the Policies tab                         */
    /* =========================================================== */
    @AuraEnabled(cacheable=true)
    public static List<Policy> getPolicies(Id contactId) {
        List<Policy> out = new List<Policy>();
        try {
            Contact c = [SELECT Email FROM Contact WHERE Id = :contactId LIMIT 1];
            if (String.isBlank(c.Email)) return out;
            String uid = resolveUnifiedId(contactId, c.Email);
            if (String.isBlank(uid)) return out;
            String safeUid = uid.replace('\'', '\\\'');

            List<Map<String, Object>> rows = runSql(
                'SELECT Id__c, PolicyNumber__c, ProductName__c, ProductCategory__c, ' +
                'PremiumMonthly__c, PremiumAnnual__c, CoverageAmount__c, ' +
                'StartDate__c, EndDate__c, Status__c ' +
                'FROM InsurancePolicy__dlm WHERE PartyId__c = \'' + safeUid + '\' ' +
                'ORDER BY StartDate__c DESC LIMIT 20');

            if (rows != null) {
                for (Map<String, Object> r : rows) {
                    Policy p = new Policy();
                    p.id              = sv(r, 'Id__c');
                    p.policyNumber    = sv(r, 'PolicyNumber__c');
                    p.productName     = sv(r, 'ProductName__c');
                    p.productCategory = sv(r, 'ProductCategory__c');
                    p.premiumMonthly  = nv(r, 'PremiumMonthly__c');
                    p.premiumAnnual   = nv(r, 'PremiumAnnual__c');
                    p.coverageAmount  = nv(r, 'CoverageAmount__c');
                    String sd = sv(r, 'StartDate__c');
                    p.startDate = (sd != null && sd.length() >= 10) ? sd.substring(0, 10) : sd;
                    String ed = sv(r, 'EndDate__c');
                    p.endDate = (ed != null && ed.length() >= 10) ? ed.substring(0, 10) : ed;
                    p.status          = sv(r, 'Status__c');
                    out.add(p);
                }
            }
        } catch (Exception ex) { System.debug('getPolicies: ' + ex.getMessage()); }
        return out;
    }

    /* =========================================================== */
    /* Claims — list for the Claims tab                             */
    /* =========================================================== */
    @AuraEnabled(cacheable=true)
    public static List<Claim> getClaims(Id contactId) {
        List<Claim> out = new List<Claim>();
        try {
            Contact c = [SELECT Email FROM Contact WHERE Id = :contactId LIMIT 1];
            if (String.isBlank(c.Email)) return out;
            String uid = resolveUnifiedId(contactId, c.Email);
            if (String.isBlank(uid)) return out;
            String safeUid = uid.replace('\'', '\\\'');

            List<Map<String, Object>> rows = runSql(
                'SELECT Id__c, PolicyId__c, ClaimDate__c, ClaimType__c, ' +
                'ClaimAmount__c, Status__c, ResolutionDate__c ' +
                'FROM InsuranceClaim__dlm WHERE PartyId__c = \'' + safeUid + '\' ' +
                'ORDER BY ClaimDate__c DESC LIMIT 15');

            if (rows != null) {
                for (Map<String, Object> r : rows) {
                    Claim cl = new Claim();
                    cl.id             = sv(r, 'Id__c');
                    cl.policyId       = sv(r, 'PolicyId__c');
                    String cd = sv(r, 'ClaimDate__c');
                    cl.claimDate = (cd != null && cd.length() >= 10) ? cd.substring(0, 10) : cd;
                    cl.claimType      = sv(r, 'ClaimType__c');
                    cl.claimAmount    = nv(r, 'ClaimAmount__c');
                    cl.status         = sv(r, 'Status__c');
                    String rd = sv(r, 'ResolutionDate__c');
                    cl.resolutionDate = (rd != null && rd.length() >= 10) ? rd.substring(0, 10) : rd;
                    out.add(cl);
                }
            }
        } catch (Exception ex) { System.debug('getClaims: ' + ex.getMessage()); }
        return out;
    }

    /* =========================================================== */
    /* Email Engagement — list for the Engagement tab               */
    /* =========================================================== */
    @AuraEnabled(cacheable=true)
    public static List<EngagementEvent> getEngagement(Id contactId) {
        List<EngagementEvent> out = new List<EngagementEvent>();
        try {
            Contact c = [SELECT Email FROM Contact WHERE Id = :contactId LIMIT 1];
            if (String.isBlank(c.Email)) return out;
            String uid = resolveUnifiedId(contactId, c.Email);
            if (String.isBlank(uid)) return out;
            String safeUid = uid.replace('\'', '\\\'');

            List<Map<String, Object>> rows = runSql(
                'SELECT Id__c, CampaignName__c, SentDate__c, EmailAddress__c, ' +
                'Opened__c, Clicked__c, Unsubscribed__c ' +
                'FROM EmailEngagement__dlm WHERE PartyId__c = \'' + safeUid + '\' ' +
                'ORDER BY SentDate__c DESC LIMIT 20');

            if (rows != null) {
                for (Map<String, Object> r : rows) {
                    EngagementEvent e = new EngagementEvent();
                    e.id           = sv(r, 'Id__c');
                    e.campaignName = sv(r, 'CampaignName__c');
                    String sd = sv(r, 'SentDate__c');
                    e.sentDate = (sd != null && sd.length() >= 10) ? sd.substring(0, 10) : sd;
                    e.emailAddress = sv(r, 'EmailAddress__c');
                    // Data Cloud returns booleans as strings "true"/"false"
                    e.opened       = 'true'.equalsIgnoreCase(sv(r, 'Opened__c'));
                    e.clicked      = 'true'.equalsIgnoreCase(sv(r, 'Clicked__c'));
                    e.unsubscribed = 'true'.equalsIgnoreCase(sv(r, 'Unsubscribed__c'));
                    out.add(e);
                }
            }
        } catch (Exception ex) { System.debug('getEngagement: ' + ex.getMessage()); }
        return out;
    }

    /* =========================================================== */
    /* Household members — reuse the same DMO pattern               */
    /* =========================================================== */
    @AuraEnabled(cacheable=false)
    public static List<HouseholdMember> getHouseholdMembers(Id contactId) {
        List<HouseholdMember> out = new List<HouseholdMember>();
        final String LINK_DMO = '__HOUSEHOLD_LINK_DMO__';
        if (LINK_DMO.startsWith('__')) return out;
        try {
            String cid = String.valueOf(contactId).replace('\'', '\\\'');
            List<Map<String, Object>> hh = runSql(
                'SELECT UnifHouseholdId__c FROM ' + LINK_DMO +
                ' WHERE SourceRecordId__c = \'' + cid + '\' LIMIT 5');
            if (hh == null || hh.isEmpty()) return out;
            Set<String> hhIds = new Set<String>();
            for (Map<String, Object> r : hh) { String h = sv(r, 'UnifHouseholdId__c'); if (!String.isBlank(h)) hhIds.add(h); }
            if (hhIds.isEmpty()) return out;
            List<String> quoted = new List<String>();
            for (String h : hhIds) quoted.add('\'' + h.replace('\'', '\\\'') + '\'');
            List<Map<String, Object>> members = runSql(
                'SELECT SourceRecordId__c FROM ' + LINK_DMO +
                ' WHERE UnifHouseholdId__c IN (' + String.join(quoted, ',') + ') LIMIT 50');
            if (members == null) return out;
            Set<Id> mids = new Set<Id>();
            for (Map<String, Object> r : members) {
                String src = sv(r, 'SourceRecordId__c');
                if (String.isBlank(src) || src == cid) continue;
                try { Id mid = Id.valueOf(src); if (mid.getSObjectType() == Contact.SObjectType) mids.add(mid); } catch (Exception e) {}
            }
            if (mids.isEmpty()) return out;
            for (Contact c : [SELECT Id, FirstName, LastName, Email, MailingCity, MailingCountry FROM Contact WHERE Id IN :mids]) {
                HouseholdMember m = new HouseholdMember();
                m.contactId = c.Id; m.firstName = c.FirstName; m.lastName = c.LastName;
                m.city = c.MailingCity; m.country = c.MailingCountry;
                out.add(m);
            }
        } catch (Exception ex) { HouseholdMember err = new HouseholdMember(); err.error = ex.getMessage(); out.add(err); }
        return out;
    }

    /* =========================================================== */
    /* Identity resolution helpers                                  */
    /* =========================================================== */
    private static String resolveUnifiedId(Id contactId, String email) {
        if (contactId != null) {
            String cid = String.valueOf(contactId);
            List<Map<String, Object>> direct = runSql(
                'SELECT UnifiedRecordId__c FROM IndividualIdentityLink__dlm ' +
                'WHERE SourceRecordId__c = \'' + cid + '\' LIMIT 1');
            if (direct != null && !direct.isEmpty()) {
                String uid = sv(direct[0], 'UnifiedRecordId__c');
                if (!String.isBlank(uid)) return uid;
            }
            if (cid.length() == 18) {
                List<Map<String, Object>> d15 = runSql(
                    'SELECT UnifiedRecordId__c FROM IndividualIdentityLink__dlm ' +
                    'WHERE SourceRecordId__c = \'' + cid.substring(0, 15) + '\' LIMIT 1');
                if (d15 != null && !d15.isEmpty()) { String uid = sv(d15[0], 'UnifiedRecordId__c'); if (!String.isBlank(uid)) return uid; }
            }
        }
        if (String.isBlank(email)) return null;
        String safeEmail = email.trim().toLowerCase().replace('\'', '\\\'');
        List<Map<String, Object>> cpe = runSql(
            'SELECT ssot__PartyId__c FROM ssot__ContactPointEmail__dlm ' +
            'WHERE LOWER(ssot__EmailAddress__c) = \'' + safeEmail + '\' LIMIT 20');
        if (cpe == null || cpe.isEmpty()) return null;
        for (Map<String, Object> row : cpe) {
            String partyId = sv(row, 'ssot__PartyId__c');
            if (String.isBlank(partyId)) continue;
            List<Map<String, Object>> link = runSql(
                'SELECT UnifiedRecordId__c FROM IndividualIdentityLink__dlm ' +
                'WHERE SourceRecordId__c = \'' + partyId.replace('\'', '\\\'') + '\' LIMIT 1');
            if (link != null && !link.isEmpty()) { String uid = sv(link[0], 'UnifiedRecordId__c'); if (!String.isBlank(uid)) return uid; }
        }
        return null;
    }

    private static List<String> resolveSourceIds(String unifiedId) {
        List<String> ids = new List<String>();
        List<Map<String, Object>> rows = runSql(
            'SELECT SourceRecordId__c FROM IndividualIdentityLink__dlm ' +
            'WHERE UnifiedRecordId__c = \'' + unifiedId.replace('\'', '\\\'') + '\' LIMIT 50');
        if (rows != null) { for (Map<String, Object> r : rows) { String s = sv(r, 'SourceRecordId__c'); if (!String.isBlank(s)) ids.add(s); } }
        return ids;
    }

    /* =========================================================== */
    /* SQL runner — Core proxy first, CDP fallback                  */
    /* =========================================================== */
    private static List<Map<String, Object>> runSql(String sql) {
        List<Map<String, Object>> rows = runSqlCore(sql);
        if (rows != null) return rows;
        return runSqlCdp(sql);
    }

    private static List<Map<String, Object>> runSqlCore(String sql) {
        ensureCdpAuth();
        if (String.isBlank(CACHED_CORE_TOKEN)) return null;
        try {
            HttpRequest req = new HttpRequest();
            req.setTimeout(30000);
            req.setEndpoint(URL.getOrgDomainUrl().toExternalForm() + '/services/data/v62.0/ssot/query');
            req.setMethod('POST');
            req.setHeader('Authorization', 'Bearer ' + CACHED_CORE_TOKEN);
            req.setHeader('Content-Type', 'application/json');
            req.setBody('{"sql":' + JSON.serialize(sql) + '}');
            HttpResponse res = new Http().send(req);
            if (res.getStatusCode() >= 200 && res.getStatusCode() < 300) return parseSsotQuery(res.getBody());
            System.debug('runSqlCore ' + res.getStatusCode() + ': ' + res.getBody());
        } catch (Exception ex) { System.debug('runSqlCore ex: ' + ex.getMessage()); }
        return null;
    }

    private static List<Map<String, Object>> runSqlCdp(String sql) {
        ensureCdpAuth();
        if (String.isBlank(CACHED_CDP_TOKEN) || String.isBlank(CACHED_CDP_TENANT)) return null;
        try {
            HttpRequest req = new HttpRequest();
            req.setTimeout(30000);
            req.setEndpoint(CACHED_CDP_TENANT + '/api/v2/query');
            req.setMethod('POST');
            req.setHeader('Authorization', 'Bearer ' + CACHED_CDP_TOKEN);
            req.setHeader('Content-Type', 'application/json');
            req.setBody('{"sql":' + JSON.serialize(sql) + '}');
            HttpResponse res = new Http().send(req);
            if (res.getStatusCode() < 200 || res.getStatusCode() >= 300) return null;
            Map<String, Object> body = (Map<String, Object>) JSON.deserializeUntyped(res.getBody());
            Object dataRaw = body.get('data');
            if (dataRaw instanceof List<Object> && body.get('metadata') != null) {
                List<Object> data = (List<Object>) dataRaw;
                Map<String, Object> meta = (Map<String, Object>) body.get('metadata');
                List<String> cols = new List<String>();
                for (Integer i = 0; i < meta.size(); i++) cols.add(null);
                for (String k : meta.keySet()) { Map<String, Object> m = (Map<String, Object>) meta.get(k); Integer pos = (Integer) m.get('placeInOrder'); if (pos != null && pos < cols.size()) cols.set(pos, k); }
                List<Map<String, Object>> out = new List<Map<String, Object>>();
                for (Object rowO : data) {
                    List<Object> row = (List<Object>) rowO;
                    Map<String, Object> rowMap = new Map<String, Object>();
                    for (Integer i = 0; i < row.size(); i++) rowMap.put(cols[i], row[i]);
                    out.add(rowMap);
                }
                return out;
            }
            return parseSsotQuery(res.getBody());
        } catch (Exception ex) { System.debug('runSqlCdp ex: ' + ex.getMessage()); }
        return null;
    }

    private static List<Map<String, Object>> parseSsotQuery(String body) {
        Map<String, Object> b = (Map<String, Object>) JSON.deserializeUntyped(body);
        Object dataRaw = b.get('data');
        if (dataRaw == null) return new List<Map<String, Object>>();
        List<Object> data = (List<Object>) dataRaw;
        List<Map<String, Object>> out = new List<Map<String, Object>>();
        for (Object r : data) { if (r instanceof Map<String, Object>) out.add((Map<String, Object>) r); }
        return out;
    }

    /* =========================================================== */
    /* Value helpers                                                */
    /* =========================================================== */
    private static String sv(Map<String, Object> row, String key) {
        if (row == null) return null;
        Object v = row.get(key);
        if (v == null) return null;
        String s = String.valueOf(v);
        return (s == 'null') ? null : s;
    }
    private static Double nv(Map<String, Object> row, String key) {
        if (row == null) return null;
        Object v = row.get(key);
        if (v == null) return null;
        try { return Double.valueOf(String.valueOf(v)); } catch (Exception e) { return null; }
    }

    /* =========================================================== */
    /* DTOs                                                         */
    /* =========================================================== */
    public class InsuranceBundle {
        @AuraEnabled public String  firstName;
        @AuraEnabled public String  lastName;
        @AuraEnabled public String  email;
        @AuraEnabled public String  phone;
        @AuraEnabled public String  city;
        @AuraEnabled public String  country;
        @AuraEnabled public String  unifiedId;
        @AuraEnabled public String  loyaltyTier;
        @AuraEnabled public Double  churnScore;       // 0–100 (gen_data field)
        @AuraEnabled public Double  ltv;              // lifetime value (gen_data field)
        @AuraEnabled public Double  npsScore;         // 0–10 (gen_data field)
        @AuraEnabled public Double  activePolicies;
        @AuraEnabled public Double  totalPolicies;
        @AuraEnabled public Double  totalPremium;
        @AuraEnabled public Double  totalCoverage;
        @AuraEnabled public Double  claimsThisYear;
        @AuraEnabled public Double  claimAmountTotal;
        @AuraEnabled public String  error;
    }

    public class Policy {
        @AuraEnabled public String id;
        @AuraEnabled public String policyNumber;
        @AuraEnabled public String productName;
        @AuraEnabled public String productCategory;
        @AuraEnabled public Double premiumMonthly;
        @AuraEnabled public Double premiumAnnual;
        @AuraEnabled public Double coverageAmount;
        @AuraEnabled public String startDate;
        @AuraEnabled public String endDate;
        @AuraEnabled public String status;
    }

    public class Claim {
        @AuraEnabled public String id;
        @AuraEnabled public String policyId;
        @AuraEnabled public String claimDate;
        @AuraEnabled public String claimType;
        @AuraEnabled public Double claimAmount;
        @AuraEnabled public String status;
        @AuraEnabled public String resolutionDate;
    }

    public class EngagementEvent {
        @AuraEnabled public String  id;
        @AuraEnabled public String  campaignName;
        @AuraEnabled public String  sentDate;
        @AuraEnabled public String  emailAddress;
        @AuraEnabled public Boolean opened;
        @AuraEnabled public Boolean clicked;
        @AuraEnabled public Boolean unsubscribed;
    }

    public class HouseholdMember {
        @AuraEnabled public String contactId;
        @AuraEnabled public String firstName;
        @AuraEnabled public String lastName;
        @AuraEnabled public String city;
        @AuraEnabled public String country;
        @AuraEnabled public String error;
    }
}
