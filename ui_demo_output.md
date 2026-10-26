
## 1. this month total sales
*metric card + delta badge*
- **period**: 01 Oct 2026 to 07 Oct 2026
- **metric_card** Total sales: `₹60.03 lakh`
  - delta `+70.4%` trend `up` comparison `vs same span last month`
  - subtext `Transactions: 37`
- **sources**: {'type': 'sources', 'items': ['Sales Report']}
- **suggestions**: ['Compare with last month', 'Break it down by customer full name']

## 2. today comp dia ctw
*glossary: ctw*
- **period**: 07 Oct 2026
- **metric_card** Company diamond carat weight: `26.000 ctw`
  - delta `-45.5%` trend `down` comparison `vs yesterday`
  - subtext `Transactions: 8`
- **sources**: {'type': 'sources', 'items': ['Sales Report']}
- **suggestions**: ['Compare with last month', 'Break it down by customer full name']

## 3. today total diamind wt and pcs
*glossary: ctw + pcs*
- **period**: 07 Oct 2026
- **heading**: {'type': 'heading', 'content': 'today total diamind wt and pcs'}
- **table** `None` (2 rows)
  - ['Diamond weight', '307.104 ctw']
  - ['Diamond pieces', '775 pcs']
- **text**: None
- **sources**: {'type': 'sources', 'items': ['Order Report']}
- **glossary**: {'ctw': 'Carat Total Weight — total carat weight of stones in a piece', 'pcs': 'Pieces — count of items'}
- **suggestions**: ['Compare with last month', 'Break it down by job customercode']

## 4. top 5 design that perform in this month
*insights severity + dead stock warning*
- **period**: 01 Oct 2026 to 07 Oct 2026
- **heading**: {'type': 'heading', 'content': 'Revenue by Design No'}
- **table** `None` (5 rows)
  - ['1', '794', '₹11.66 lakh']
  - ['2', '813', '₹8.07 lakh']
  - ['3', 'zrv4', '₹4.59 lakh']
  - ['4', '815', '₹3.51 lakh']
  - ...
- **metric**: {'type': 'metric', 'content': 'Total revenue for top 5: ₹31.01 lakh', 'raw_value': 3100779.84, 'unit': 'currency', 'unit_label': '', 'label': 'Total revenue'}
- **chart**: {'type': 'chart', 'chart_type': 'bar', 'x_key': 'Design No', 'series': [{'name': 'Revenue', 'data': [{'x': '794', 'y': 1165900.0}, {'x': '813', 'y': 806660.0}, {'x': 'zrv4', 'y': 4
- **sources**: {'type': 'sources', 'items': ['Sales Report']}
- **list** `Insights`
  - [info] Top 3 designnos contribute 78% of the total
  - [info] 794 (₹11.66 lakh) is 2.4x the average of the other designnos
  - [warning] Dead stock: 1,236 of 1,854 designs that ever sold had no sales in the last 90 days (67%)
- **suggestions**: ['Same breakdown for last month', 'Show top 10 designnos', 'Why did 794 perform well?']

## 5. jobs by department
*aging warning + suggestions*
- **heading**: {'type': 'heading', 'content': 'Count by Department'}
- **table** `None` (5 rows)
  - ['1', 'Pending Request', '2,946']
  - ['2', 'Sprue Cutting-Receive', '289']
  - ['3', 'setting-Issue', '242']
  - ['4', 'Pre Polish-Issue', '160']
  - ...
- **metric**: {'type': 'metric', 'content': 'Total count for top 5: 3,791', 'raw_value': 3791.0, 'unit': 'count', 'unit_label': '', 'label': 'Total count'}
- **chart**: {'type': 'chart', 'chart_type': 'bar', 'x_key': 'Department', 'series': [{'name': 'Count', 'data': [{'x': 'Pending Request', 'y': 2946.0}, {'x': 'Sprue Cutting-Receive', 'y': 289.0
- **sources**: {'type': 'sources', 'items': ['Wip Report']}
- **list** `Insights`
  - [info] Top 3 departments contribute 92% of the total
  - [info] Pending Request alone is 78% of the total
  - [info] Pending Request (2,946) is 13.9x the average of the other departments
  - [info] Pending Request is a clear outlier — 10.2x the next department
  - [warning] Aging: 4,532 of 4,792 jobs over 7 days old (95%) — worst department: Pending Request (2,723)
- **suggestions**: ['Show top 10 Departments', 'Why did Pending Request perform well?']

## 6. how many overdue orders
*glossary none (just count)*
- **metric_card** Overdue orders: `1,057`
  - subtext `Transactions: 6,044`
- **sources**: {'type': 'sources', 'items': ['Order Report']}
- **suggestions**: ['Break it down by job customercode']

## 7. what is gold rate today
*market context block*
- **period**: 07 Oct 2026
- **metric_card** Metal rate: `10,500.00`
  - delta `-34.5%` trend `down` comparison `vs yesterday`
  - subtext `Transactions: 8`
- **sources**: {'type': 'sources', 'items': ['Sales Report']}
- **suggestions**: ['Compare with last month', 'Break it down by customer full name']
- **market_context**: {'type': 'market_context', 'note': "I don't have a live market-data source connected right now, so I can't report a live market rate — the value below is your *billed* metal rate from invoices — not t...

## 8. tell me company wise sales
*table + suggestions*
- **period**: customer=company
- **text**: None
- **sources**: {'type': 'sources', 'items': ['Sales Report']}
- **suggestions**: ['Show top 10 customer full names']