# The deck, the raise, and who PayPilot is actually competing with

Read 2026-10-17. Event is roughly 2026-11-17. Story meeting 2026-10-18.

Every figure here comes either from the deck's own text, read with
`pdftotext -layout` from
`~/Downloads/PayPilot_Clean_SelfContained_Full_Deck_Fixed_Slide12.pdf`, or
from a page opened first hand and recorded in `docs/claims.md` rows 23 to 30.
Nothing is asserted from a search summary without saying so.

## 1. The deck contradicts itself, and it is the first thing a numerate reader finds

Slide 6: fifteen production customers create EUR 225k ARR, at a EUR 15k
annual contract.

Slide 10: M18 target EUR 300k ARR. M24, fifteen customers.

EUR 300k at EUR 15k is twenty customers, not fifteen. So the deck promises
more ARR at month 18 than at month 24, and ARR falls in between. Each slide
is internally fine, which is why nobody caught it: the defect only exists
when the two are read together, and an investor reads them together because
slide 6 is the ask and slide 10 is the proof.

Fix one of the three numbers. The cheapest fix is slide 10: make M18 a
customer count rather than an ARR figure, because a customer count is a
milestone the company controls and an ARR figure is a product of two
assumptions.

## 2. The raise arithmetic is the real problem, and no competitor finding changes it

EUR 750k asked for. EUR 225k ARR promised at month 24. That is roughly 0.3x
of ARR per euro invested, over a 24-month deployment, which works out at
about EUR 31k a month of spend.

The consequence is a question, not an opinion. A company at EUR 225k ARR
raises its next round against that ARR. At any ordinary multiple, the
company is worth low single-digit millions at the moment the EUR 750k
instrument is supposed to convert. Slide 11 says only that "the signed
documents define the economics", which is true and is the sentence an
investor stops on, because the economics are exactly what the pair of
numbers makes difficult.

Two ways out and they point in opposite directions.

**Shrink the raise.** EUR 150k to EUR 250k is an angel or pre-seed cheque,
it matches a EUR 225k ARR milestone, and it is raisable from the room on
17 November rather than from a fund. It also matches the actual spend: one
founder, paid pilots, integrations. The milestone table then reads as
conservative rather than as inconsistent.

**Or grow the plan.** EUR 750k expects a seed-stage milestone, which is
roughly EUR 1m to EUR 1.5m ARR at month 24. That needs either many more
customers or a much larger contract, and the deck currently argues for
neither. Larger contracts are the more credible of the two, because the
pricing slide already hints at it: "flat annual fee by failed-payment
volume" scales with the customer, and a EUR 2M-monthly-billings customer
is not the same size as a EUR 20M one.

Pick one before the story meeting. The deck cannot ask for a seed amount
against a pre-seed plan, and that mismatch is not a presentation problem.

## 3. The two companies in the brief are in a different market, and that is the honest answer

Both were read first hand on 2026-10-17.

**Stuut** (`stuut.ai`, not `stuut.com`, which is a parked domain for sale)
is enterprise order-to-cash. Headline "Your AI coworker for order-to-cash".
Six modules: Collections, Cash Application, Payments, Disputes &
Deductions, Order Management, Credit. Twelve named logos including
Honeywell, ZoomInfo, PerkinElmer, Verifone, NCR Voyix and Ally. Nineteen
named integrations across SAP, Oracle, NetSuite, Dynamics, Salesforce and
four US banks, under the position "Nothing gets replaced". Funding is
RELAYED: a reported USD 52.5M Series B led by Insight Partners with a16z
and M12, total USD 93M, announced 2026-10-07, which is ten days ago.

**Adfin** (`adfin.com`) is UK SME invoicing. "Just get paid." Essential
credit control is FREE under a fair use policy. Payments are 20p + 1%,
capped at GBP 4 on direct debit and bank payments. Enhanced credit control
is 0.3% per paid invoice. Funding is RELAYED: USD 18M Series A led by Index
Ventures, May 2026, total above USD 30M.

PayPilot is subscription card-failure recovery. The failure it addresses
has nobody present. The failure an AR product addresses is an invoice a
human has chosen not to pay yet, and that human is reachable, nameable and
chaseable. Those are different products with different buyers, and saying
so is stronger than pretending to a rivalry: an investor who knows this
space will know Stuut is enterprise AR, and claiming it as a competitor
marks the speaker as not knowing his own market.

The overlap is real and narrow and gets named rather than dismissed.
Adfin's payments tier lists "Automated fallbacks when payments fail" and
it takes cards and direct debit. Stuut carries Payments and Disputes &
Deductions. Each one touches a failed charge at the edge of its own
product. Neither is built on involuntary churn in a subscription book.
What neither of them has been checked on, and what is recorded in
`docs/claims.md` row 28 as the open question, is what each actually does
when a card declines on a recurring charge.

## 4. The free tier answers itself, and the answer is why PayPilot cannot copy it

Adfin gives credit control away because it owns the payment rail at
20p + 1%, with a further 0.3% when the AI features get used, plus premium
card rates up to 20p + 3%, GBP 15 per chargeback and GBP 36 a month to put
the merchant's own name on the direct debit instead of Adfin's. The free
product is the acquisition channel for the paid rail. Its own pricing page
says it in one line: "We only get paid if you get paid."

PayPilot has no rail. It sits on top of Stripe, Recharge and whatever else,
and it does not touch the money. So that model is structurally unavailable,
and the flat annual fee on slide 8 is the right instrument rather than a
weaker one. It is also the one to defend out loud, because the obvious
investor question is why not take a percentage of recovery, and the answer
is already in the deck: percentage pricing creates attribution disputes.
That sentence is the strongest thing on slide 8 and it is currently
buried in a PROBLEM column.

## 5. Slide 3 names the wrong competitors, and this is the finding that changes the deck

Slide 3 says "Stripe and Recharge already recover failed payments". True,
and they are platforms rather than competitors: they are what PayPilot
plugs into. The funded point solutions in this exact market are absent
from the deck, and all four are RELAYED rather than read first hand:

- **Butter Payments**, ML recovery across processors including Stripe
  Billing, Braintree and Recharge, on revenue-share pricing.
- **Gravy**, a managed human-led recovery service operating as a branded
  extension of support.
- **Churn Buster**, founded 2013, claiming 1,000+ subscription businesses
  and 50+ million failed payments handled.
- **Revaly**, formerly FlexPay, claiming recovery in 2.9 days average.

An investor who has seen one dunning pitch has seen these names. Omitting
them reads as either not knowing them or hoping nobody asks, and both are
worse than naming them and saying where PayPilot differs. The difference is
already the deck's own argument and it needs no new claim: Butter and
Revaly price on revenue share, which is the attribution dispute slide 8
already names; Gravy is people rather than a decision layer; and none of
them sells an audit trail of why each action was taken.

Read all four first hand before 17 November. Four pages, one afternoon.
Until then they are relayed and they do not go on a slide as fact.

## 6. Zero customers, and that is the gap no edit closes

Slide 10 starts at M3. So M0 is nothing: no customers, no pilots, no
revenue, and no traction slide anywhere in twelve slides. Slide 12 says
plainly that "all targets in this deck are planning assumptions until
customers validate them", which is honest and is also the investor's whole
objection in the deck's own words.

The deck already prices the thing that fixes this: a EUR 5k paid pilot,
credited against production. One signed pilot before 17 November is worth
more than every edit in this document combined, because it converts slide 7
from an illustration into a customer, and it converts slide 10 from a plan
into a first data point. It is also four weeks of work rather than four
months.

If a pilot is not signable in four weeks, the second-best thing is a
**named design partner who has agreed in writing to be named**. That is a
smaller ask than EUR 5k and it moves M3 from a projection to a fact.

## 7. The shape of the deck

Nine of twelve slides use the same PROBLEM / SOLUTION / RESULT three-column
template with three large numbers underneath. Each slide is clean. The
repetition is the problem: a reader feels the template by slide four and
stops reading the columns, which means the slides that most need to be read
closely, 6 and 8, are the ones getting the least attention.

It opens on the problem, which is right. Then slides 3, 4 and 5 are all
mechanism, and money does not arrive until slide 6. The shape that works
is problem, where the money goes, what it costs, then mechanism, with the
mechanism earning its place only once the reader wants it. Slides 4 and 5
are the two to compress.

## What to do, in order

1. Fix the slide 6 against slide 10 contradiction. Half an hour, and it is
   the one defect that costs credibility rather than opportunity.
2. Decide the raise size against the plan. This is the story meeting's
   whole agenda and it is a decision rather than a drafting exercise.
3. Replace slide 3's competitor naming with the four real ones, after
   reading them.
4. Try to sign one EUR 5k pilot, or get one named design partner in
   writing, before 17 November.
5. Compress slides 4 and 5 into one and give the room back to the money.

## What is not in here, named rather than buried

The four dunning competitors are relayed. Both funding figures are relayed.
Stuut's and Adfin's own performance claims are vendor hero figures with no
method and they are recorded so nobody re-derives them as market facts.
And nobody has yet checked what Adfin or Stuut do when a card declines on a
recurring charge, which is the single question that decides whether the
overlap in section 3 is an edge or a collision.
