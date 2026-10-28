# Two vendors, two retry windows for the same returned ACH debit

When an ACH debit comes back R01 (insufficient funds) or R09 (uncollected funds), you may send it again. The question is how many times and for how long. Two public sources I read on 28 October 2026 give different answers.

Increase's ACH returns documentation says an R01 or R09 debit can be reinitiated a maximum of two times, and that reinitiating must take place within 180 days of the settlement date of the original transfer.

Canopy's return-code FAQ lists R01 and R09 as retryable "up to 2x within 30 days of original authorization".

Same count, different clock. One window starts at settlement and runs 180 days. The other starts at authorization and runs 30.

The free primary source is Nacha's ACH Operations Bulletin #1-2014, dated 30 September 2014. It says an entry returned for insufficient or uncollected funds may be reinitiated a maximum of two times. It says reinitiation must take place within 180 days of the settlement date of the original entry, and that after that, any further action on the returned entry has to happen outside the ACH Network. It also says that language in an authorization allowing more attempts than the rules do is not permitted, and gives the example of an authorization that allows three attempts.

The rule text itself agrees with the bulletin. Supplement #2-2018 to the Nacha Operating Rules, a copy hosted by the regional association NEACH, reproduces subsection 2.12.4.1 as amended after the 2 November 2018 vote. It says the originator "must Reinitiate the Entry within 180 days after the Settlement Date of the original Entry", and "must not Reinitiate an Entry that has been returned for insufficient or uncollected funds more than two times following the Return of the original Entry". The amendment was an editorial change to make clear that the cap of two had been the intent all along.

The same subsection lists debits that are not treated as reinitiated entries. One is a debit that is one in a series of preauthorized, recurring debits and does not depend on whether an earlier debit in the series was returned. Under that wording, next month's debit is not a reinitiation, so neither window governs it.

Both documents predate today. The current Operating Rules text is sold; I have not read it, so I make no claim that this text is in force now.

I do not know why Canopy counts 30 days from authorization. A processor can enforce a tighter house limit than the rules, and that would be a reasonable thing to do. Calling it the rule is a mistake, because the 2018 text I read contains no authorization-based clock.

How to check this yourself: the bulletin, the 2018 supplement and both vendor pages are linked below, and each quoted sentence can be found on them. Where a vendor page and the rule text disagree, trust neither summary until you have the current rule text.

- Nacha, ACH Operations Bulletin #1-2014, 30 September 2014: https://www.nacha.org/system/files/2023-03/ACH-Operations-Bulletin-%231-2014%20Questionable%20ACH%20Debit%20Origination_0.pdf
- Increase, ACH returns: https://increase.com/documentation/ach-returns.html
- Canopy, ACH payment return codes FAQ: https://support.getcanopy.com/en/articles/9671827-faq-ach-payment-return-codes-chargebacks
- Supplement #2-2018 to the Nacha Operating Rules, as hosted by NEACH: https://www.neach.org/Portals/0/Supplement%20%232-2018%20converted%20nov%2019%202018.pdf
