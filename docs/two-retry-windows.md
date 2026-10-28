# Two vendors, two retry windows for the same returned ACH debit

When an ACH debit comes back R01 (insufficient funds) or R09 (uncollected funds), you may send it again. The question is how many times and for how long. Two public sources I read on 28 October 2026 give different answers.

Increase's ACH returns documentation says an R01 or R09 debit can be reinitiated a maximum of two times, and that reinitiating must take place within 180 days of the settlement date of the original transfer.

Canopy's return-code FAQ lists R01 and R09 as retryable "up to 2x within 30 days of original authorization".

Same count, different clock. One window starts at settlement and runs 180 days. The other starts at authorization and runs 30.

The free primary source is Nacha's ACH Operations Bulletin #1-2014, dated 30 September 2014. It says an entry returned for insufficient or uncollected funds may be reinitiated a maximum of two times. It says reinitiation must take place within 180 days of the settlement date of the original entry, and that after that, any further action on the returned entry has to happen outside the ACH Network. It also says that language in an authorization allowing more attempts than the rules do is not permitted, and gives the example of an authorization that allows three attempts.

That settles which vendor matches the bulletin. It does not settle what the rule says today. The bulletin is twelve years old, and the current Nacha Operating Rules text is sold, so I have not read it.

I do not know why Canopy says 30 days from authorization. A processor can enforce a tighter house limit than the rules, and that would be a reasonable thing to do. Presenting it as the rule is the problem. For a recurring debit, a window counted from a standing authorization could close before the second month's return ever arrives. That last point is my reading of those clocks, not something either source states.

How to check this yourself: the Nacha bulletin and both vendor pages are linked below, and each sentence above can be found on them. If a vendor page and the bulletin disagree, trust neither summary until you have the current rule text.

- Nacha, ACH Operations Bulletin #1-2014, 30 September 2014: https://www.nacha.org/system/files/2023-03/ACH-Operations-Bulletin-%231-2014%20Questionable%20ACH%20Debit%20Origination_0.pdf
- Increase, ACH returns: https://increase.com/documentation/ach-returns.html
- Canopy, ACH payment return codes FAQ: https://support.getcanopy.com/en/articles/9671827-faq-ach-payment-return-codes-chargebacks
