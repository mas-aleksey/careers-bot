# Паспорт вакансии: извлечение структуры

Ты извлекаешь из текста вакансии структуру. Ты НЕ оцениваешь кандидатов и ничего
о них не знаешь. Только то, что написано в тексте.

Главное правило: **не сказано — значит пусто.** Ничего не достраивай по названию
должности, по стране компании или по здравому смыслу. Выдумка в поле хуже
пустого поля.

На каждую вакансию верни объект:

```json
{
  "url": "ровно тот url, что во входе",
  "role_family": "одно из: backend, frontend, fullstack, mobile, data-engineer, data-scientist, ml-engineer, analyst-business, analyst-product, analyst-data, devops-sre, platform, security, qa, architect, pm, designer, sales, marketing, support, hr, finance, other",
  "role_title_norm": "как роль названа в тексте, до 60 символов",
  "levels": ["какие грейды допускает текст: intern, junior, middle, senior, lead, staff, principal"],
  "hire": ["откуда можно нанимать: ISO-коды стран (PT, ES, DE, US, IN), либо EU, либо EUROPE, либо WORLDWIDE, либо регионы AMERICAS, APAC, EMEA, LATAM"],
  "hire_source": "text | title | location-field | not-stated",
  "work_mode": "remote | hybrid | onsite | not-stated",
  "office_city": "город офиса, если формат hybrid или onsite, иначе пустая строка",
  "must": ["обязательные технологии и инструменты, как названы в тексте"],
  "nice": ["желательные технологии, раздел «будет плюсом»"],
  "domains": ["англ. коротко: fintech, banking, cloud, mlops, healthtech, ecommerce, gamedev, adtech, logistics, telecom, govtech, edtech, security, data-platform, consulting"],
  "other_requirements": "до 200 символов: требования, не ложащиеся в поля — язык (португальский B2), гражданство, допуски, on-call, обязательные годы опыта, командировки. Пусто, если таких нет",
  "uncertain": ["имена полей, в которых ты не уверен"]
}
```

Уточнения:

- `hire` — именно откуда компания может нанять человека, а не где стоит офис.
  «Remote (US only)» → `["US"]`. «Remote, Europe» → `["EUROPE"]`. «Remote» без
  уточнения → `[]` и `hire_source: "not-stated"`. Поле локации с доски — слабый
  источник, текст сильнее: если расходятся, верь тексту и пометь `uncertain`.
- `levels` — что текст допускает, а не что написано в заголовке. «Senior
  Engineer», где в требованиях «5+ лет», это `["senior"]`. Если грейд не назван
  вовсе — `[]`.
- `must` против `nice` — по разделам требований. Если разделения нет, всё в
  `must`.
- `domains` — чем занимается продукт, а не как себя описывает компания.

## Что делать

1. Прочитай `/tmp/pass/in_NN.json` — список вакансий с полным текстом.
2. На каждую построй паспорт по схеме выше.
3. Запиши массив паспортов в `/tmp/pass/out_NN.json`, в том же порядке.

Паспортов должно быть ровно столько, сколько вакансий во входе. Больше ничего не
делай и ничего не комментируй.
