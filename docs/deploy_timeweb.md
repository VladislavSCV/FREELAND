# Деплой на Timeweb Cloud VPS

## 1. Сервер

- VPS с Ubuntu 22.04/24.04, от 2 ГБ RAM.
- DNS: A-запись домена (например `os.example.ru`) указывает на IP сервера.
- Открыты порты 80 и 443. Порт 8000 наружу не нужен: API привязан к `127.0.0.1`.

```bash
curl -fsSL https://get.docker.com | sh
git clone <repo> freelance-os && cd freelance-os
cp .env.example .env
```

## 2. `.env`

Обязательно задайте:

| Переменная | Значение |
|---|---|
| `POSTGRES_PASSWORD` и пароль в `DATABASE_URL` | один и тот же длинный пароль |
| `API_TOKEN`, `WEBHOOK_TOKEN` | `python3 -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `LLM_PROVIDER` + ключ | `openai` + `OPENAI_API_KEY` или `deepseek` + `DEEPSEEK_API_KEY` |
| `TELEGRAM_BOT_TOKEN`, `ALLOWED_TELEGRAM_IDS` | токен от @BotFather, ваш числовой ID (узнать у @userinfobot) |
| `DOMAIN`, `WEBHOOK_DOMAIN` | `os.example.ru` и `https://os.example.ru` |
| `LETSENCRYPT_EMAIL` | e-mail для уведомлений Let's Encrypt |
| `TELEGRAM_CHANNELS` | публичные каналы с заказами, через запятую, без `@` |

Прямые запросы к OpenAI с российских IP блокируются. Используйте DeepSeek или OpenAI-совместимый шлюз (`LLM_BASE_URL`).

## 3. Запуск

```bash
docker compose up -d --build   # postgres, redis, api (+миграции), worker, nginx, certbot
docker compose exec api freelance seed
docker compose logs -f api worker certbot
```

- Пока certbot не получил сертификат, nginx отдаёт временный самоподписанный. Certbot получает сертификат в течение нескольких минут и повторяет попытку каждые 5 минут, пока не получит. Nginx переключается на настоящий сертификат сам. Продление проверяется дважды в день.
- При старте API регистрирует Telegram webhook `https://DOMAIN/api/v1/telegram/webhook`. Пока сертификат временный, Telegram не может доставить обновления и копит их у себя; после выпуска настоящего сертификата они придут сами. Проверить webhook: `curl https://api.telegram.org/bot<TOKEN>/getWebhookInfo`.
- Проверка: `https://DOMAIN/health` и `https://DOMAIN/docs`; в боте — `/status`.

## 4. Платёжные webhooks (когда подключите эквайринг)

- Prodamus: URL уведомлений `https://DOMAIN/api/v1/webhooks/prodamus`, `PRODAMUS_SECRET_KEY` — секретный ключ из кабинета. В ссылке на оплату передавайте `order_num=project-<id>`.
- ЮKassa: HTTP-уведомление `payment.succeeded` на `https://DOMAIN/api/v1/webhooks/yookassa`, задайте `YOOKASSA_SHOP_ID` и `YOOKASSA_SECRET_KEY`. При создании платежа передавайте `metadata.project_id=<id>`. Уведомления ЮKassa не подписаны, поэтому каждый платёж перепроверяется запросом к API ЮKassa.
- Пока `MANUAL_PAYMENT_MODE=true`, платежи из webhooks только записываются (`GET /api/v1/payments`), а статус проекта меняет `/pay_project`. Чтобы проекты переходили в `PAID` автоматически, выставьте `false`.

## 5. Обслуживание

```bash
git pull && docker compose up -d --build                    # обновление (миграции применяются при старте API)
docker compose exec postgres pg_dump -U freelance freelance > backup.sql
docker compose exec api freelance scrape                     # внеочередной сбор заказов
```
