# Data Processing Agreement template

This is a template for the Data Processing Agreement (DPA) between the client and Streamflow Solutions.
It positions **Streamflow Solutions as the processor** and the **client as the controller**.
The processing rests on the controller's **legitimate interest and contract** with its own customers; it does **not** rely on fresh consent, and PayPilot runs no consent flow.

This template is drafting support, not legal advice.
Both parties should have it reviewed before signing.
Replace every `[BRACKETED]` field and complete the schedules.

Bracketed placeholders to complete:

- `[CLIENT LEGAL NAME]`, `[CLIENT ADDRESS]` - the controller.
- `[EFFECTIVE DATE]` - the start date.
- `[RETENTION MONTHS]` - the retention window, default 12 months.
- `[PROCESSING REGION]` - e.g. UK/EU (London).
- `[SUBPROCESSOR LIST]` - see Schedule 2.

---

## Data Processing Agreement

This Data Processing Agreement ("DPA") is entered into on [EFFECTIVE DATE] between:

- **[CLIENT LEGAL NAME]**, of [CLIENT ADDRESS] (the "**Controller**"), and
- **Streamflow Solutions** (the "**Processor**"),

each a "party" and together the "parties".

It governs the Processor's processing of personal data on the Controller's behalf through the PayPilot failed-payment recovery service.

### 1. Roles

- The Controller determines the purposes and means of the processing and is the controller of the personal data.
- The Processor processes the personal data only on the Controller's documented instructions, as the Controller's processor.
- This DPA does not create a consent relationship with data subjects; the Controller's lawful basis is its legitimate interest in recovering its own revenue and the performance of its contract with its customers.

### 2. Subject matter, duration, nature, and purpose

- **Subject matter:** processing of personal data to recover failed subscription payments on the Controller's behalf.
- **Duration:** for the term of the service agreement, plus the retention window in Schedule 1.
- **Nature and purpose:** receiving payment-failure events, drafting and sending a recovery message, and recording whether the invoice was recovered.

### 3. Categories of data subject and personal data

See Schedule 1.
In summary, the data subjects are the Controller's own customers whose subscription payment failed, and the personal data is limited to identifiers and billing metadata, plus a name and email used transiently to send one message.
No card data is processed.

### 4. Controller instructions

- The Processor processes personal data only on the Controller's documented instructions, including as set out in this DPA and the service configuration.
- The Processor informs the Controller if, in its opinion, an instruction infringes applicable data-protection law.

### 5. Confidentiality

- The Processor ensures that persons authorized to process the personal data are bound by confidentiality.

### 6. Security measures

The Processor implements appropriate technical and organizational measures, including:

- single-tenant deployment per Controller, with the Controller's credentials and ledger isolated from any other deployment,
- encryption in transit (TLS) for all traffic and outbound API calls,
- least-privilege credentials: a restricted payment-processor key scoped only to what recovery needs,
- an audit trail of money-affecting and authentication events,
- masking of direct identifiers before any model processing, and non-retention of name and email in the ledger,
- fail-closed guards on outbound messages.

At-rest encryption of the storage volume and the associated evidence is addressed in the readiness documentation and in Schedule 3 (open items).

### 7. Sub-processors

- The Controller authorizes the Processor to engage the sub-processors listed in Schedule 2.
- The Processor imposes data-protection obligations on each sub-processor no less protective than those in this DPA.
- The Processor informs the Controller of any intended change of sub-processor and gives the Controller the opportunity to object.

### 8. Data-subject rights

- The Processor assists the Controller, by appropriate technical and organizational measures and insofar as possible, in fulfilling the Controller's obligation to respond to data-subject requests.
- The service provides scripted export and erasure paths keyed on the customer identifier, and a scheduled retention purge, so an access, portability, or erasure request resolves to a definite set of records.

### 9. Personal-data breach

- The Processor notifies the Controller without undue delay after becoming aware of a personal-data breach and provides the information the Controller needs to meet its own notification obligations.

### 10. International transfers

- The Processor processes the personal data in the region stated in Schedule 1 and does not transfer it outside that region without an appropriate transfer mechanism and the Controller's authorization.

### 11. Deletion and return

- On termination, and on a valid erasure request, the Processor deletes or returns the personal data in accordance with the retention window in Schedule 1, unless retention is required by law.

### 12. Audits

- The Processor makes available to the Controller the information necessary to demonstrate compliance with this DPA, including the control documentation, and allows for and contributes to audits on reasonable notice.

---

## Schedule 1 - Details of processing

- **Data subjects:** the Controller's customers whose subscription payment failed.
- **Categories of personal data:**
  - identifiers (customer id, payment-processor customer id, subscription id, invoice id),
  - billing metadata (amount, currency, failure reason, recovery state, timestamps),
  - name and email, used transiently to send one recovery message and not retained in the ledger.
- **Special-category data:** none.
- **Card data:** none; payment is taken only on payment-processor-hosted pages.
- **Retention:** [RETENTION MONTHS] months after an invoice reaches a terminal state, then deletion.
- **Processing region:** [PROCESSING REGION].

## Schedule 2 - Authorized sub-processors

| Sub-processor | Purpose | Region |
| --- | --- | --- |
| Stripe | Payment processing and billing portal | [SUBPROCESSOR LIST] |
| Resend | Email delivery | [SUBPROCESSOR LIST] |
| [HOSTING PROVIDER] | Application hosting and storage volume | [PROCESSING REGION] |

## Schedule 3 - Open items

- At-rest encryption of the storage volume is to be confirmed and evidenced (see `docs/compliance/soc2-readiness.md`, gaps list).
- The export, erasure, and retention-purge scripts are being implemented; this DPA is written against their intended behavior.

---

**Signed for the Controller:** ______________________  Date: __________

**Signed for the Processor (Streamflow Solutions):** ______________________  Date: __________
</content>
