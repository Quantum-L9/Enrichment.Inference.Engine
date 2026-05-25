# CRM DATA READINESS CHECKLIST
## Engineering & RevOps Implementation Guide

**Purpose:** Ensure CRM data is optimized for AI-powered advertising (Google Ads, Salesforce Agentforce) and complies with privacy regulations.

**Target Audience:** Engineering teams, RevOps, Data Engineers, Marketing Ops

**Success Criteria:**
- Google Customer Match lists achieve >50% match rates
- Enhanced Conversions capture >80% of offline revenue
- Salesforce Data Cloud can unify >90% of customer records
- Data quality score averages >75/100

---

## PHASE 1: DATA AUDIT & ASSESSMENT

### 1.1 Inventory Current State
**Task:** Map all systems containing customer data

- [ ] **List all data sources**
  - CRM system (e.g., Salesforce, HubSpot, custom)
  - Transactional database (ERP, POS, billing)
  - Marketing platforms (email, ad platforms, analytics)
  - Support systems (ticketing, chat logs)
  - E-commerce platform (if applicable)
  - Offline data (spreadsheets, paper records)

- [ ] **Document schema for each system**
  - Field names and data types
  - Primary keys and foreign keys
  - Update frequency (real-time, hourly, daily, manual)
  - Data retention policies

- [ ] **Identify integration points**
  - Which systems currently sync with each other?
  - What's the data flow direction?
  - Sync frequency and method (API, ETL, CSV export/import)

### 1.2 Assess Data Quality Baseline
**Task:** Measure current state across six quality dimensions

- [ ] **Accuracy Check**
  - Sample 500 random records
  - Manually verify email deliverability (use email validation API)
  - Test phone numbers (format check, area code validation)
  - Verify addresses (USPS API or equivalent)
  - Calculate accuracy rate: `(valid_records / total_records) * 100`
  - **Target:** >90% accuracy

- [ ] **Completeness Check**
  - Query for null/empty critical fields:
    ```sql
    SELECT
      COUNT(*) as total_records,
      SUM(CASE WHEN email IS NULL OR email = '' THEN 1 ELSE 0 END) as missing_email,
      SUM(CASE WHEN phone IS NULL OR phone = '' THEN 1 ELSE 0 END) as missing_phone,
      SUM(CASE WHEN first_name IS NULL THEN 1 ELSE 0 END) as missing_first_name,
      SUM(CASE WHEN last_name IS NULL THEN 1 ELSE 0 END) as missing_last_name,
      SUM(CASE WHEN address IS NULL THEN 1 ELSE 0 END) as missing_address
    FROM contacts;
    ```
  - Calculate completeness per field: `(populated / total) * 100`
  - **Target:** >85% for email, >70% for phone

- [ ] **Consistency Check**
  - Find format inconsistencies:
    - Phone: `+1-555-123-4567` vs `(555) 123-4567` vs `5551234567`
    - State: `California` vs `CA` vs `Calif.`
    - Country: `USA` vs `US` vs `United States`
  - Document all format variations per field
  - **Target:** <5% format inconsistency rate

- [ ] **Uniqueness Check (Duplicate Detection)**
  - Run duplicate detection queries:
    ```sql
    -- Exact email duplicates
    SELECT email, COUNT(*) as count
    FROM contacts
    WHERE email IS NOT NULL
    GROUP BY email
    HAVING COUNT(*) > 1;

    -- Fuzzy name + address matches
    SELECT
      first_name, last_name, postal_code, COUNT(*) as count
    FROM contacts
    GROUP BY first_name, last_name, postal_code
    HAVING COUNT(*) > 1;
    ```
  - Calculate duplicate rate: `(duplicate_records / total_records) * 100`
  - **Target:** <3% duplicate rate

- [ ] **Timeliness Check**
  - Query for stale data:
    ```sql
    SELECT
      COUNT(*) as total,
      SUM(CASE WHEN last_updated < NOW() - INTERVAL '1 year' THEN 1 ELSE 0 END) as stale_1yr,
      SUM(CASE WHEN last_updated < NOW() - INTERVAL '6 months' THEN 1 ELSE 0 END) as stale_6mo
    FROM contacts;
    ```
  - **Target:** <10% records stale >1 year

- [ ] **Validity Check**
  - Test data against validation rules:
    - Email regex: `^[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}$`
    - Phone format (should be E.164 or convertible)
    - Date fields (no future dates for historical events)
    - Postal codes (match country format)
  - **Target:** >95% pass validation rules

### 1.3 Consent & Compliance Audit
**Task:** Verify legal readiness for AI ad targeting

- [ ] **Marketing consent tracking**
  - Does CRM have explicit `marketing_consent` field? (Yes/No)
  - Is consent timestamp captured?
  - Is consent source/method documented? (web form, in-person, email)
  - What % of database has explicit consent?
    ```sql
    SELECT
      COUNT(*) as total,
      SUM(CASE WHEN marketing_consent = TRUE THEN 1 ELSE 0 END) as consented,
      (SUM(CASE WHEN marketing_consent = TRUE THEN 1 ELSE 0 END) * 100.0 / COUNT(*)) as consent_rate
    FROM contacts;
    ```
  - **Target:** >60% consent rate for active customers

- [ ] **Opt-out mechanism audit**
  - How do users opt out? (email link, phone, web portal)
  - How fast do opt-outs sync to CRM? (real-time, daily batch)
  - Are opt-outs synced to ad platforms within 24 hours?
  - **Requirement:** Must sync within 24 hours per GDPR/CCPA

- [ ] **Data retention policy**
  - Is there a documented policy for how long to keep PII?
  - Are inactive, non-consented records purged automatically?
  - **Recommendation:** Purge PII 2 years after last activity for non-consented users

- [ ] **Consent for specific channels**
  - Email opt-in tracked separately? (required for email marketing)
  - SMS opt-in tracked? (required for text marketing)
  - Cookie consent tracked? (required for retargeting in EU)

---

## PHASE 2: SCHEMA STANDARDIZATION

### 2.1 Identity Fields Normalization
**Task:** Standardize fields Google/Salesforce use for matching

- [ ] **Email standardization**
  - Convert all emails to lowercase
  - Remove leading/trailing whitespace
  - Validate format with regex
  - Mark invalid emails with flag (don't delete yet)
  - Create `email_verified` boolean field
  - **Script template:**
    ```sql
    UPDATE contacts
    SET email_primary = LOWER(TRIM(email_primary)),
        email_verified = CASE
          WHEN email_primary ~ '^[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}$'
          THEN TRUE ELSE FALSE
        END;
    ```

- [ ] **Phone standardization to E.164 format**
  - Target format: `+[country_code][number]` (e.g., `+12125551234`)
  - Remove all non-numeric characters except leading `+`
  - Add country code if missing (default to +1 for US)
  - Validate length (US = 12 chars including +1)
  - Create `phone_verified` boolean field
  - **Script template (pseudo-code):**
    ```python
    import phonenumbers

    def normalize_phone(phone, default_country='US'):
        try:
            parsed = phonenumbers.parse(phone, default_country)
            return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
        except:
            return None

    # Apply to all phone fields in batch
    ```

- [ ] **Name field separation**
  - Split full names into `first_name`, `last_name` if stored as single field
  - Extract suffixes (Jr, Sr, III) into separate `name_suffix` field
  - Standardize capitalization (Title Case)
  - **Requirement:** Google Customer Match needs separate first/last name

- [ ] **Address standardization**
  - Validate addresses using USPS API (US) or equivalent
  - Standardize state to two-letter codes (California → CA)
  - Standardize country to ISO 3166-1 alpha-2 (United States → US)
  - Extract 5-digit or 9-digit ZIP codes (remove extensions if present)
  - Create separate fields: `address_line_1`, `address_line_2`, `city`, `state`, `postal_code`, `country_code`

### 2.2 Add Required Google/Salesforce Fields
**Task:** Ensure schema supports all integration requirements

- [ ] **Google Enhanced Conversions fields**
  - Add to transaction/order table:
    - `gclid` VARCHAR(255) — Google Click ID
    - `gbraid` VARCHAR(255) — Google Ads browsing ID
    - `wbraid` VARCHAR(255) — Walled garden browsing ID
    - `conversion_action_id` VARCHAR(50)
    - `conversion_value` DECIMAL(10,2)
    - `conversion_reported` BOOLEAN DEFAULT FALSE
    - `conversion_reported_date` TIMESTAMP

- [ ] **Consent tracking fields**
  - Add to contact/account tables:
    - `marketing_consent` BOOLEAN DEFAULT FALSE
    - `marketing_consent_date` TIMESTAMP
    - `marketing_consent_source` VARCHAR(100)
    - `email_opt_in` BOOLEAN DEFAULT FALSE
    - `sms_opt_in` BOOLEAN DEFAULT FALSE
    - `data_processing_consent` BOOLEAN DEFAULT FALSE
    - `consent_ip_address` VARCHAR(45)
    - `consent_user_agent` TEXT

- [ ] **Data quality tracking fields**
  - Add to contact table:
    - `email_verified` BOOLEAN DEFAULT FALSE
    - `phone_verified` BOOLEAN DEFAULT FALSE
    - `address_verified` BOOLEAN DEFAULT FALSE
    - `data_quality_score` DECIMAL(5,2)
    - `is_duplicate` BOOLEAN DEFAULT FALSE
    - `duplicate_of_contact_id` VARCHAR(36)

- [ ] **Attribution tracking fields**
  - Add to contact/account tables:
    - `acquisition_source` VARCHAR(100) — "google_ads", "facebook", "referral"
    - `acquisition_campaign` VARCHAR(255)
    - `first_gclid` VARCHAR(255) — Click ID from first visit
    - `last_gclid` VARCHAR(255) — Most recent click ID
    - `utm_source` VARCHAR(255)
    - `utm_medium` VARCHAR(255)
    - `utm_campaign` VARCHAR(255)

- [ ] **AI model feature fields**
  - Add behavioral aggregates to contact table:
    - `ltv_predicted` DECIMAL(10,2) — Predicted lifetime value
    - `propensity_to_return` DECIMAL(5,4) — 0-1 score
    - `lifecycle_stage` VARCHAR(50) — "lead", "active", "dormant", "churned"
    - `customer_tier` VARCHAR(20) — "bronze", "silver", "gold", "platinum"
    - `total_transactions` INT DEFAULT 0
    - `total_revenue` DECIMAL(10,2) DEFAULT 0
    - `days_since_last_purchase` INT

- [ ] **Salesforce sync tracking**
  - Add to all synced tables:
    - `salesforce_id` VARCHAR(18)
    - `salesforce_sync_status` VARCHAR(50)
    - `salesforce_last_sync` TIMESTAMP

### 2.3 Create Staging Tables
**Task:** Build export queues for Google/Salesforce integration

- [ ] **Create `conversion_export_queue` table**
  - Purpose: Stage offline conversions for Google Enhanced Conversions API
  - Schema: See sample schema document (Section 4)
  - Include fields for retry logic and error tracking

- [ ] **Create `customer_match_export` table**
  - Purpose: Track Google Customer Match list uploads
  - Include: segment name, export date, match rate, Google list ID

- [ ] **Create `consent_log` table**
  - Purpose: Audit trail for all consent changes
  - Include: timestamp, consent type, old/new status, IP address, method

---

## PHASE 3: DEDUPLICATION & MERGE

### 3.1 Define Deduplication Rules
**Task:** Document merge logic before executing

- [ ] **Define match criteria priority:**
  1. **Exact email match** → Automatic merge (highest confidence)
  2. **Phone + Last Name match** → Review required
  3. **Address + Last Name match** → Review required
  4. **Fuzzy name match only** → Flag for manual review

- [ ] **Define master record selection rules:**
  - Most recent `last_activity_date` wins
  - OR: Record with highest `data_quality_score` wins
  - OR: Record with `salesforce_id` (if exists) wins
  - Document your rule clearly

- [ ] **Define merge strategy per field:**
  - Email: Keep both if different (primary + secondary)
  - Phone: Keep both if different
  - Address: Keep most recent
  - Transactions: Merge all child records to master
  - Consent: Use most permissive (if either TRUE, result TRUE)

### 3.2 Execute Deduplication
**Task:** Merge duplicate records systematically

- [ ] **Phase 1: Exact email duplicates (automated)**
  ```sql
  -- Identify duplicates
  CREATE TEMP TABLE email_dupes AS
  SELECT email_primary, MIN(contact_id) as master_id, ARRAY_AGG(contact_id) as all_ids
  FROM contacts
  WHERE email_primary IS NOT NULL
  GROUP BY email_primary
  HAVING COUNT(*) > 1;

  -- Mark duplicates
  UPDATE contacts
  SET is_duplicate = TRUE,
      duplicate_of_contact_id = ed.master_id
  FROM email_dupes ed
  WHERE contacts.email_primary = ed.email_primary
    AND contacts.contact_id != ed.master_id;
  ```

- [ ] **Phase 2: Phone + name matches (review required)**
  - Export matches to CSV for manual review
  - Marketing/sales team validates which are true duplicates
  - Data engineer executes approved merges

- [ ] **Phase 3: Merge child records**
  - Update all transactions to point to master `contact_id`
  - Update all consent logs
  - Update all activity history

- [ ] **Phase 4: Archive or soft-delete duplicates**
  - Option A: Soft delete (set `is_active = FALSE`, keep for audit)
  - Option B: Move to `contacts_archived` table
  - **Never hard delete before backing up**

- [ ] **Validate merge results**
  - Re-run duplicate detection queries
  - Verify child record counts match pre-merge
  - Check that no master records were accidentally deleted

---

## PHASE 4: CLICK ID CAPTURE & ATTRIBUTION

### 4.1 Website Tracking Setup
**Task:** Capture Google Click IDs (gclid) for Enhanced Conversions

- [ ] **Add URL parameter capture to website**
  - Capture `gclid`, `gbraid`, `wbraid` from URL query string
  - Store in cookie or session storage (90-day expiry)
  - **Sample JavaScript:**
    ```javascript
    function getURLParameter(name) {
      const urlParams = new URLSearchParams(window.location.search);
      return urlParams.get(name);
    }

    const gclid = getURLParameter('gclid');
    if (gclid) {
      // Store in cookie
      document.cookie = `gclid=${gclid}; max-age=7776000; path=/`;
      // Or store in sessionStorage
      sessionStorage.setItem('gclid', gclid);
    }
    ```

- [ ] **Pass click ID to CRM on form submission**
  - Include hidden form fields: `gclid`, `gbraid`, `wbraid`
  - Or: Send via AJAX to server endpoint
  - Map to CRM fields: `first_gclid` (if new contact) and `last_gclid` (always update)

- [ ] **Pass click ID to transaction/order table**
  - When user completes purchase/conversion, include `gclid` in order record
  - This enables linking offline conversions back to ad clicks

- [ ] **UTM parameter capture**
  - Also capture: `utm_source`, `utm_medium`, `utm_campaign`, `utm_content`, `utm_term`
  - Store in contact record for attribution reporting

### 4.2 Offline Conversion Tracking
**Task:** Link in-person transactions to online ad clicks

- [ ] **Add click ID to in-person checkout flow**
  - Option A: Ask customer for email → lookup contact → retrieve stored `last_gclid`
  - Option B: Customer scans QR code on arrival → `gclid` captured from URL
  - Option C: Loyalty program login → `gclid` retrieved from profile

- [ ] **Store click ID in transaction record**
  - Every transaction should have `gclid` field populated when available
  - Also store `contact_id` for backup Enhanced Conversions matching

---

## PHASE 5: CONSENT MANAGEMENT

### 5.1 Implement Consent Capture
**Task:** Build mechanisms to collect and document consent

- [ ] **Web form consent checkboxes**
  - Add explicit checkbox: "I agree to receive marketing communications"
  - Separate checkboxes for: Email, SMS, Phone
  - Add pre-checked GDPR opt-in (for EU visitors): "I consent to data processing"
  - **Never pre-check marketing opt-ins**

- [ ] **Capture consent metadata**
  - On form submit, store:
    - `marketing_consent = TRUE/FALSE`
    - `marketing_consent_date = CURRENT_TIMESTAMP`
    - `marketing_consent_source = 'web_form'`
    - `consent_ip_address` (for proof)
    - `consent_user_agent` (browser info)

- [ ] **In-person consent capture**
  - Printed form with checkboxes for marketing consent
  - Or: Tablet-based form at checkout
  - Store: `marketing_consent_source = 'in_person'`

- [ ] **Double opt-in for email (recommended)**
  - Send confirmation email with verification link
  - Only set `email_verified = TRUE` after click
  - This improves deliverability and compliance

### 5.2 Build Consent Management Interface
**Task:** Allow customers to manage preferences

- [ ] **Preference center webpage**
  - URL like: `yoursite.com/email-preferences?email={email}`
  - Display current consent status
  - Allow user to opt in/out of each channel independently
  - Show consent history (when granted, via what method)

- [ ] **Unsubscribe link in emails**
  - One-click unsubscribe (no login required)
  - Updates CRM: `email_opt_in = FALSE`, logs to `consent_log`
  - Must sync to ad platforms within 24 hours

- [ ] **GDPR data deletion request flow**
  - Web form or email address for requests
  - Process: anonymize or delete PII within 30 days
  - Keep audit log of deletion requests

### 5.3 Sync Consent to Ad Platforms
**Task:** Ensure opt-outs are respected across all systems

- [ ] **Daily consent sync to Google Ads**
  - Export list of opted-out emails/phones
  - Upload to Google Ads as suppression list
  - Remove from all Customer Match lists

- [ ] **Daily consent sync to Salesforce**
  - Update `marketing_consent` field in Salesforce Contact/Lead
  - Salesforce Data Cloud respects this for segment activation

- [ ] **Monitor sync failures**
  - Alert if sync fails for >24 hours (compliance violation)

---

## PHASE 6: GOOGLE ADS INTEGRATION

### 6.1 Enhanced Conversions Setup
**Task:** Send offline conversions to Google with PII for matching

- [ ] **Create conversion actions in Google Ads**
  - Define: "Purchase", "Lead", "Qualified Lead", etc.
  - Enable "Enhanced conversions" for each action
  - Note `conversion_action_id` for each

- [ ] **Build Enhanced Conversions export script**
  - Query transactions with `conversion_reported = FALSE`
  - For each transaction:
    - Hash PII (email, phone, name) with SHA-256
    - Format payload per Google API spec
    - Include `gclid` if available, else hashed PII
  - **Sample Python script structure:**
    ```python
    import hashlib
    from google.ads.googleads.client import GoogleAdsClient

    def hash_value(value):
        return hashlib.sha256(value.strip().lower().encode()).hexdigest()

    # Build conversion payload
    conversion = {
        'conversion_action': f'customers/{customer_id}/conversionActions/{action_id}',
        'conversion_date_time': transaction['date'].isoformat(),
        'conversion_value': float(transaction['amount']),
        'currency_code': 'USD',
        'user_identifiers': [
            {'hashed_email': hash_value(contact['email'])},
            {'hashed_phone_number': hash_value(contact['phone'])}
        ]
    }

    # If gclid available, add it
    if transaction['gclid']:
        conversion['gclid'] = transaction['gclid']

    # Send to Google Ads API
    ```

- [ ] **Automate export schedule**
  - Run every 4 hours via cron/Airflow/Lambda
  - Update `conversion_reported = TRUE` after successful send
  - Log failures to `conversion_export_queue` for retry

- [ ] **Validate Enhanced Conversions in Google Ads UI**
  - Go to Tools > Conversions > click conversion action
  - Check "Enhanced conversions" shows as "Eligible"
  - Monitor match rate (should be >50% if data is clean)

### 6.2 Customer Match List Setup
**Task:** Create targetable audience lists in Google Ads

- [ ] **Define audience segments**
  - High-value customers: `ltv_predicted > 5000 AND total_transactions >= 10`
  - Lapsed customers: `days_since_last_purchase BETWEEN 90 AND 180`
  - New leads: `lifecycle_stage = 'lead' AND created_at > NOW() - INTERVAL '30 days'`

- [ ] **Export segment to CSV per Google format**
  - Required columns: `Email`, `Phone`, `First Name`, `Last Name`, `Country`, `Zip`
  - Hash PII with SHA-256 before upload (or let Google hash)
  - **Sample export query:**
    ```sql
    SELECT
      SHA2(LOWER(TRIM(email_primary)), 256) AS Email,
      SHA2(REGEXP_REPLACE(phone_mobile, '[^0-9+]', ''), 256) AS Phone,
      SHA2(LOWER(first_name), 256) AS "First Name",
      SHA2(LOWER(last_name), 256) AS "Last Name",
      country_code AS Country,
      postal_code AS Zip
    FROM contacts
    WHERE marketing_consent = TRUE
      AND email_verified = TRUE
      AND data_quality_score >= 70
      AND ltv_predicted > 5000;
    ```

- [ ] **Upload to Google Ads**
  - Tools > Audience Manager > Customer List
  - Upload CSV, set match key (email + phone recommended)
  - Wait 24-48 hours for matching to complete
  - Check match rate (goal: >50%)

- [ ] **Create similar audiences (lookalike)**
  - In Audience Manager, select Customer Match list
  - Create "Similar audience" with 1-10% similarity
  - Use for prospecting campaigns

- [ ] **Automate list refresh**
  - Re-upload segment weekly or monthly
  - Google Ads automatically updates list membership
  - Track upload history in `customer_match_export` table

### 6.3 Google Ads Account Prerequisites
**Task:** Ensure account meets Customer Match eligibility

- [ ] **Verify account meets requirements:**
  - 90+ days history in Google Ads ✓
  - $50,000+ total lifetime spend ✓
  - Good policy compliance history ✓
  - Good payment history ✓

- [ ] **Enable "Targeting" setting for Customer Match**
  - Without this, can only use for bid adjustments, not targeting
  - Only available to accounts meeting spend/compliance requirements

---

## PHASE 7: SALESFORCE DATA CLOUD INTEGRATION

### 7.1 Salesforce Data Cloud Setup
**Task:** Configure Data Cloud to unify CRM data

- [ ] **Connect data sources to Data Cloud**
  - Salesforce Sales Cloud (automatic)
  - External database via Salesforce Connect or MuleSoft
  - Marketing Cloud via native connector
  - Google Analytics via connector

- [ ] **Map data to Data Cloud data model**
  - Map CRM `contacts` table → Data Cloud `Individual` object
  - Map transactions → Data Cloud `Engagement` events
  - Define schema: fields, data types, relationships

- [ ] **Configure identity resolution rules**
  - Primary match key: `email_primary`
  - Secondary match keys: `phone_mobile`, `contact_id`, `salesforce_id`
  - Set reconciliation rules (which value wins on conflict)
  - **Start conservative:** Exact email match only
  - **Validate in Profile Explorer:** Check for over-merging (different people incorrectly unified)
  - **Expand gradually:** Add phone, address matching once validated

- [ ] **Build unified profiles**
  - Run identity resolution job (may take hours for large datasets)
  - Review unified profiles in Profile Explorer
  - Calculate unification rate: `(unified_records / total_records) * 100`
  - **Target:** >90% unification rate

### 7.2 Create Calculated Insights
**Task:** Build AI-ready features in Data Cloud

- [ ] **Calculated attributes (aggregations)**
  - Total purchases last 90 days
  - Average order value
  - Days since last purchase
  - Purchase frequency
  - Product category preferences

- [ ] **Predictive insights (AI models)**
  - Lifetime value prediction (built-in Data Cloud model)
  - Churn propensity (custom model or Einstein Discovery)
  - Next best product recommendation
  - Propensity to respond to email

- [ ] **Segmentation**
  - Build segments based on calculated insights
  - Example: "High LTV, Low Engagement" = `ltv_predicted > 5000 AND days_since_last_activity > 60`

### 7.3 Activate Data to Ad Platforms
**Task:** Push segments from Salesforce to Google Ads

- [ ] **Configure Data Cloud activation destination: Google Ads**
  - In Data Cloud, set up Google Ads as activation target
  - Authenticate with Google account
  - Map Data Cloud segments → Google Customer Match lists

- [ ] **Activate segment to Google Ads**
  - Select segment (e.g., "High LTV Customers")
  - Choose activation: Google Ads Customer Match
  - Set refresh cadence (daily, weekly)
  - Data Cloud automatically uploads list and keeps it synced

- [ ] **Activate segment to other platforms**
  - Meta (Facebook/Instagram) via Conversions API connector
  - Google DV360
  - Trade Desk (via LiveRamp or UID2)

---

## PHASE 8: DATA QUALITY MONITORING

### 8.1 Build Data Quality Dashboard
**Task:** Create automated monitoring of quality metrics

- [ ] **Implement quality score calculation**
  - Formula:
    ```sql
    data_quality_score = (
      (CASE WHEN email_verified THEN 25 ELSE 0 END) +
      (CASE WHEN phone_verified THEN 25 ELSE 0 END) +
      (CASE WHEN address_verified THEN 20 ELSE 0 END) +
      (CASE WHEN total_transactions > 0 THEN 20 ELSE 0 END) +
      (CASE WHEN marketing_consent THEN 10 ELSE 0 END)
    )
    ```
  - Run nightly batch job to update scores

- [ ] **Create dashboard with KPIs:**
  - Average data quality score (target: >75)
  - % records with email verified (target: >85%)
  - % records with phone verified (target: >70%)
  - Duplicate rate (target: <3%)
  - % records with marketing consent (target: >60%)
  - Stale record rate (>1 year no activity) (target: <10%)

- [ ] **Set up automated alerts**
  - Alert if avg quality score drops below 70
  - Alert if duplicate rate exceeds 5%
  - Alert if email verification rate drops below 80%
  - Alert if consent sync to Google fails for >24 hours

### 8.2 Implement Validation Rules
**Task:** Prevent bad data from entering CRM

- [ ] **Email validation rule**
  - Reject on insert/update if email doesn't match regex
  - Or: Allow insert but set `email_verified = FALSE`

- [ ] **Phone validation rule**
  - Reject if phone can't be parsed to E.164
  - Or: Auto-format to E.164 on insert

- [ ] **Required field enforcement**
  - Require: `first_name`, `last_name`, `email` OR `phone`
  - Reject records missing all contact methods

- [ ] **Duplicate prevention on insert**
  - Check for existing email before insert
  - If exists, reject or update existing record instead

### 8.3 Ongoing Data Cleaning Schedule
**Task:** Automate continuous data hygiene

- [ ] **Nightly jobs:**
  - Email verification (API call to email validation service)
  - Phone normalization (convert to E.164)
  - Data quality score recalculation

- [ ] **Weekly jobs:**
  - Duplicate detection and flagging
  - Stale record flagging (>1 year no activity)
  - Address validation (USPS API)

- [ ] **Monthly jobs:**
  - Consent audit (any records with consent but no consent date?)
  - Manual review of flagged duplicates
  - Purge PII for records meeting deletion criteria

---

## PHASE 9: COMPLIANCE & GOVERNANCE

### 9.1 Document Data Policies
**Task:** Create written data governance policies

- [ ] **Data retention policy**
  - How long is PII kept?
  - When are inactive records purged?
  - Exception: Keep transaction history even if PII purged (anonymized)

- [ ] **Data access policy**
  - Who can access PII?
  - Is access logged for audit?
  - RBAC (role-based access control) implemented?

- [ ] **Data processing agreements**
  - DPA with Google (for Enhanced Conversions, Customer Match)
  - DPA with Salesforce (for Data Cloud)
  - DPA with any other vendors processing PII

### 9.2 Implement Audit Logging
**Task:** Track all changes to sensitive data

- [ ] **Log all consent changes to `consent_log` table**
  - Every time `marketing_consent` changes, insert log record
  - Include: timestamp, old value, new value, source, IP address

- [ ] **Log all data exports**
  - Track every Customer Match list export
  - Track every Enhanced Conversion API call
  - Include: timestamp, record count, destination, user

- [ ] **Log data deletion requests**
  - GDPR "right to be forgotten" requests
  - Include: timestamp, requestor info, completion date

### 9.3 Privacy Impact Assessment
**Task:** Conduct PIA for AI ad use case

- [ ] **Document data flows**
  - What data is collected?
  - Where is it stored?
  - Who has access?
  - Where is it sent (Google, Salesforce, etc.)?

- [ ] **Assess risks**
  - What could go wrong? (data breach, unauthorized access)
  - What's the impact on individuals?
  - What's the likelihood?

- [ ] **Mitigations**
  - Encryption at rest and in transit
  - Access controls
  - Regular security audits
  - Incident response plan

---

## PHASE 10: VALIDATION & GO-LIVE

### 10.1 Pre-Launch Testing
**Task:** Validate all integrations before full rollout

- [ ] **Test Enhanced Conversions end-to-end**
  - Create test transaction with known `gclid`
  - Verify it appears in `conversion_export_queue`
  - Verify it sends to Google Ads API successfully
  - Check Google Ads UI shows conversion

- [ ] **Test Customer Match list upload**
  - Export small segment (100-500 records)
  - Upload to Google Ads
  - Verify match rate is acceptable (>30% minimum, >50% good)
  - Create test campaign targeting the list

- [ ] **Test Salesforce Data Cloud unification**
  - Pick 10 known customers with activity across systems
  - Check unified profile includes all expected data
  - Verify no over-merging (different people unified)

- [ ] **Test consent opt-out flow**
  - Submit opt-out via web form
  - Verify CRM updates within seconds/minutes
  - Verify sync to Google Ads within 24 hours
  - Verify user removed from active campaigns

### 10.2 Performance Baseline
**Task:** Establish pre-AI benchmarks for comparison

- [ ] **Document current ad performance**
  - Google Ads: CPA, ROAS, conversion rate (before Enhanced Conversions)
  - Facebook: CPA, ROAS (before CAPI integration)
  - Overall: Customer acquisition cost, LTV

- [ ] **Set success criteria for post-launch**
  - Target: +20-30% ROAS improvement (based on industry benchmarks)
  - Target: +30% conversion rate improvement
  - Target: -20% CPA reduction

### 10.3 Phased Rollout
**Task:** Launch incrementally to minimize risk

- [ ] **Phase 1: Enable Enhanced Conversions (week 1-2)**
  - Turn on for all campaigns
  - Monitor match rate and conversion count
  - Expected: Conversion count may increase as offline conversions are captured

- [ ] **Phase 2: Launch Customer Match campaigns (week 3-4)**
  - Start with high-value customer segment
  - Modest budget ($500-$1000/day test)
  - Measure incremental ROAS vs control group

- [ ] **Phase 3: Enable AI bidding with first-party data (week 5-8)**
  - Switch campaigns to Target ROAS or Target CPA with value-based bidding
  - Feed first-party LTV data into bid decisions
  - Google AI optimizes toward profitable customers, not just volume

- [ ] **Phase 4: Scale based on results (week 9+)**
  - If hitting target KPIs, increase budget
  - Expand to more segments
  - Replicate to other ad platforms (Meta, etc.)

---

## SUCCESS METRICS & KPIs

### Data Quality Metrics
| Metric | Baseline | Target (3 months) | Current |
|--------|----------|-------------------|---------|
| Avg Data Quality Score | ___ | >75 | ___ |
| Email Verified % | ___ | >85% | ___ |
| Phone Verified % | ___ | >70% | ___ |
| Duplicate Rate | ___ | <3% | ___ |
| Marketing Consent % | ___ | >60% | ___ |
| Stale Records (>1yr) | ___ | <10% | ___ |

### Integration Metrics
| Metric | Target | Current |
|--------|--------|---------|
| Google Customer Match Rate | >50% | ___ |
| Enhanced Conversions Match Rate | >60% | ___ |
| Salesforce Unification Rate | >90% | ___ |
| Consent Sync Latency | <24 hours | ___ |
| Conversion Export Uptime | >99% | ___ |

### Business Impact Metrics
| Metric | Baseline | Target | Current |
|--------|----------|--------|---------|
| Google Ads ROAS | ___ | +30% | ___ |
| Google Ads CPA | ___ | -20% | ___ |
| Conversion Rate | ___ | +30% | ___ |
| Customer LTV | ___ | +15% | ___ |
| Ad Platform Spend Efficiency | ___ | +25% | ___ |

---

## TOOLS & RESOURCES

### Data Validation APIs
- **Email:** ZeroBounce, NeverBounce, Mailgun Email Validation
- **Phone:** Twilio Lookup API, Numverify, AbstractAPI
- **Address:** USPS Web Tools API, SmartyStreets, Google Maps Geocoding API

### ETL/Integration Tools
- **Cloud:** Fivetran, Stitch, Airbyte, Meltano
- **On-premise:** Talend, Pentaho, Apache NiFi
- **Salesforce-specific:** MuleSoft, Jitterbit, Informatica

### Data Quality Tools
- **Open source:** Great Expectations (Python), dbt (data testing)
- **Commercial:** Ataccama, Informatica Data Quality, Talend Data Quality
- **Salesforce native:** Duplicate Management, Data.com Clean

### Google Ads APIs
- **Enhanced Conversions:** [Google Ads API - ConversionUploadService](https://developers.google.com/google-ads/api/docs/conversions/upload-conversions)
- **Customer Match:** [Google Ads API - UserDataService](https://developers.google.com/google-ads/api/docs/remarketing/audience-segments/customer-match)

### Salesforce APIs
- **Data Cloud:** [Salesforce Data Cloud APIs](https://developer.salesforce.com/docs/atlas.en-us.c360a_api.meta/c360a_api/)
- **Marketing Cloud:** [Journey Builder API](https://developer.salesforce.com/docs/marketing/marketing-cloud/guide/journey-builder.html)

---

## APPENDIX: COMMON PITFALLS

### ❌ Mistake 1: Hashing incorrectly for Google
- **Problem:** Hashing email with uppercase, spaces, or without lowercase normalization
- **Fix:** Always: `lowercase → trim whitespace → SHA-256`

### ❌ Mistake 2: Phone format mismatch
- **Problem:** Google expects E.164 format, but CRM stores `(555) 123-4567`
- **Fix:** Normalize all phones to `+1XXXXXXXXXX` before hashing

### ❌ Mistake 3: Exporting non-consented users
- **Problem:** Violates GDPR/CCPA, leads to fines and list suspension
- **Fix:** Always filter `WHERE marketing_consent = TRUE` in export queries

### ❌ Mistake 4: Not capturing gclid
- **Problem:** Enhanced Conversions can't match offline conversions to clicks
- **Fix:** Add gclid capture to all web forms and pass through to CRM

### ❌ Mistake 5: Ignoring data quality
- **Problem:** Low match rates, wasted ad spend targeting wrong people
- **Fix:** Dedupe, validate, verify before exporting to ad platforms

### ❌ Mistake 6: One-time setup, no maintenance
- **Problem:** Data degrades over time; match rates drop, compliance violations
- **Fix:** Automate nightly quality checks, weekly deduplication, monthly audits

---

## SIGN-OFF CHECKLIST

Before declaring "data readiness" complete, verify:

- [ ] ✓ Data quality score averages >75/100
- [ ] ✓ Duplicate rate <3%
- [ ] ✓ >60% of active customers have marketing consent documented
- [ ] ✓ Email/phone normalization is automated
- [ ] ✓ Google Enhanced Conversions is live and reporting >60% match rate
- [ ] ✓ Google Customer Match lists achieve >50% match rate
- [ ] ✓ Salesforce Data Cloud unifies >90% of customer records
- [ ] ✓ Consent opt-outs sync to all platforms within 24 hours
- [ ] ✓ Data quality dashboard is live and monitored
- [ ] ✓ Governance policies are documented and approved
- [ ] ✓ All stakeholders trained on new processes

**Approved by:**
- [ ] Data Engineering Lead: ________________ Date: ________
- [ ] RevOps Manager: ________________ Date: ________
- [ ] Marketing Ops: ________________ Date: ________
- [ ] Legal/Compliance: ________________ Date: ________

---

**Next Steps After Data Readiness:**
1. Launch AI-powered ad campaigns (Performance Max, Target ROAS)
2. Implement Salesforce Agentforce for autonomous customer outreach
3. Build predictive LTV models on unified data
4. Expand to additional ad platforms (Meta CAPI, Trade Desk)
5. Monthly performance review and optimization

**Document Version:** 1.0
**Last Updated:** March 8, 2026
**Owner:** Engineering & RevOps Teams
