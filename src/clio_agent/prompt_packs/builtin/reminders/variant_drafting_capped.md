---
id: clio.reminder.variant_drafting_capped
title: Drafting note: capped count
description: The variant_drafting note's remark that fewer drafts run than were asked for; a space precedes it.
x_kind: reminder
x_active: true
x_trigger: source variant_drafting: the capped piece when more drafts were asked for than the limit allows
x_variables: requested, n
x_required: a piece of the variant_drafting note; switch that note off instead
---
({{requested}} asked, capped at {{n}})
