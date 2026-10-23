# AI Trading Agent для MetaTrader 5

Агент анализирует рынок по нескольким таймфреймам, формирует сигналы по документированным правилам,
проверяет каждую сделку детерминированным риск-модулем и — только если это разрешено режимом —
отправляет ордера в MetaTrader 5 с проверкой фактического исполнения.

> **Статус.** Проект проверен на Mock-терминале MT5 и автоматических тестах (150 тестов).
> **Реальное подключение к MT5 ещё не проверено**: среда разработки — macOS, где пакет
> `MetaTrader5` не устанавливается. Стратегия **не валидирована** на реальных данных брокера.
> LIVE выключен по умолчанию и не может включиться без нескольких явных действий.

## Архитектура

```
 Dashboard (React)  ──HTTPS──>  Django REST API  ──>  PostgreSQL
                                      │  пишет команды/настройки
                                      v
                          run_agent (один процесс, advisory lock)
                                      │
             TradingAgent: анализ → стратегия → AI (вето) → Risk Manager → исполнение
                                      │
          MT5Gateway:  mock  |  mt5 (Windows, пакет MetaTrader5)  |  remote (HTTP-мост)
                                                                         │
                                                       bridge/mt5_bridge.py на Windows + MT5
```

* Веб-API **никогда не обращается к MT5 напрямую**: оно пишет команды (`AgentCommand`) и настройки, а
  процесс `run_agent` применяет их, выполняет циклы и публикует состояние. Блокировка PostgreSQL
  гарантирует, что работает только один процесс агента.
* Разделение ответственности:

| Модуль | Файл | Что делает | Чего не может |
|---|---|---|---|
| Подключение | `trading_engine/mt5_connector.py`, `remote_gateway.py`, `mock_mt5.py` | данные, ордера, ошибки → типизированные исключения | — |
| Анализ | `market_analyzer.py`, `indicators.py` | EMA 20/50/200, RSI, MACD, ATR, ADX, HH/HL/LH/LL, уровни, пробои/ретесты, тиковый объём | использует только закрытые бары |
| Стратегия | `strategy_engine.py` | сигнал + список проверок с объяснением | не считает лот, не отправляет ордера |
| AI | `ai_decision_engine.py` | режим рынка, оценка качества, причины, учёт прошлых тестов | только вето; нет доступа к лимитам и ордерам |
| Риск | `risk_manager.py` | все лимиты, расчёт лота, подпись одобрения (HMAC, TTL 10 с) | — |
| Исполнение | `order_executor.py` | `order_check` → `order_send` → проверка позиции, идемпотентность | ничего не отправит без подписи риск-модуля |
| Позиции | `position_manager.py` | сверка журнала с MT5 каждый цикл, выходы | не трогает ручные позиции |
| Бэктест | `backtester.py`, `metrics.py` | издержки, walk-forward, критерии приёмки | — |
| Оркестрация | `agent.py` | режимы, Start/Pause/Stop/Emergency | — |

## Структура

```
.
├── trading_engine/          ядро без Django (можно тестировать и запускать отдельно)
├── bridge/mt5_bridge.py     HTTP-мост для Windows-хоста с MT5
├── backend/
│   ├── config/              settings, urls
│   ├── apps/accounts        роли viewer/operator/admin, вход по токену
│   ├── apps/market_data     символы брокера, последние снимки анализа
│   ├── apps/strategies      параметры стратегии, сигналы, прогнозы AI
│   ├── apps/risk            лимиты риска, дневное состояние
│   ├── apps/execution       журнал ордеров/сделок, команды, раннер агента
│   ├── apps/analytics       бэктесты, отчёты
│   ├── apps/audit           аудит действий, системный журнал, фильтр секретов
│   └── tests/               API и интеграция раннера с PostgreSQL
├── frontend/                React + TypeScript (Vite)
├── tests/engine/            тесты ядра
├── deployment/              Dockerfile'ы, nginx
├── docs/                    отчёт по бэктесту, переход в LIVE
├── docker-compose.yml, .env.example
├── SECURITY.md, TESTING.md
```

## Установка (macOS / Linux, без MT5 — Mock-терминал)

Нужны Python 3.11+ (проверено на 3.12), Node 20+, PostgreSQL 14+.

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env            # заполните DJANGO_SECRET_KEY, DATABASE_URL, ADMIN_PASSWORD

cd backend
python manage.py migrate
python manage.py bootstrap_roles        # группы ролей + admin из ADMIN_USERNAME/ADMIN_PASSWORD
python manage.py runserver 127.0.0.1:8000
```

Во втором терминале — агент, в третьем — дашборд:

```bash
cd backend && python manage.py run_agent          # шлюз по умолчанию: mock
cd frontend && npm ci && npm run dev              # http://127.0.0.1:5173
```

Если PostgreSQL не установлен, для разработки можно поднять встроенный (пакет `pgserver` из
`requirements-dev.txt`), см. TESTING.md.

### Docker Compose

```bash
cp .env.example .env    # задайте POSTGRES_PASSWORD, DJANGO_SECRET_KEY, ADMIN_PASSWORD
docker compose up --build
# дашборд: http://127.0.0.1:8080   API: http://127.0.0.1:8000/api/
```

MT5 внутри Linux-контейнеров не работает: в Docker используйте шлюз `mock` или `remote`.
`docker-compose.yml` не запускался в среде разработки (Docker недоступен) — проверьте его у себя.

## Подключение к демо-счёту MT5

**Вариант A — всё на одном Windows-компьютере.**

1. Установите MetaTrader 5, войдите в **демо-счёт** брокера, включите *Сервис → Настройки →
   Советники → Разрешить алгоритмическую торговлю*.
2. Установите Python, поддерживаемый пакетом `MetaTrader5` (см. PyPI), и зависимости:
   `pip install -r requirements.txt MetaTrader5`.
3. В `.env`: `MT5_LOGIN`, `MT5_PASSWORD`, `MT5_SERVER` (и `MT5_PATH`, если терминалов несколько).
4. В дашборде: *Connection → Terminal access = mt5*, сохраните, перезапустите `run_agent`.

**Вариант B — Django/дашборд на Mac/Linux/Docker, MT5 на Windows VPS.**

1. На Windows: MT5 с демо-счётом, затем
   ```bat
   pip install -r requirements-bridge.txt
   set MT5_LOGIN=... & set MT5_PASSWORD=... & set MT5_SERVER=...
   set BRIDGE_TOKEN=<случайная строка 32+ символа>
   set BRIDGE_ALLOW_REAL=false
   python -m uvicorn bridge.mt5_bridge:app --host 127.0.0.1 --port 8765 --ssl-keyfile key.pem --ssl-certfile cert.pem
   ```
   (каталоги `trading_engine/` и `bridge/` скопируйте на VPS).
2. Доступ к мосту — только через TLS и/или VPN (WireGuard/Tailscale) или SSH-туннель
   (`ssh -L 8765:127.0.0.1:8765 vps`). Не открывайте порт в интернет.
3. На стороне Django: `MT5_BRIDGE_URL`, `MT5_BRIDGE_TOKEN` (и `MT5_BRIDGE_CA_FILE` для
   самоподписанного сертификата); в дашборде *Terminal access = remote*; перезапустите `run_agent`.

**После подключения:**

1. *Market analysis* должна показывать реальные котировки. Если символ «not offered by broker» —
   укажите точное имя (например `EURUSD.m`) в *Connection → Symbols*.
2. *Connection → Trading mode → Demo*. Агент проверит, что терминал сообщает **демо-счёт**;
   иначе режим не включится.
3. Нажмите **Start**. Все сигналы, отказы и исполнения видны в *Trading signals* и *System logs*.

## Запуск и остановка

| Действие | Как | Что происходит |
|---|---|---|
| Start | кнопка / `POST /api/control/command/ {"command":"start"}` | новые входы разрешены (если режим и риск позволяют) |
| Pause | кнопка | анализ и сопровождение позиций продолжаются, новых входов нет |
| Stop | кнопка | циклы не выполняются |
| Emergency stop | кнопка (с подтверждением) | kill switch: новые сделки заблокированы, отложенные ордера агента отменены, **позиции не закрываются**; переживает перезапуск; сброс — только admin |
| Закрыть позиции агента | *Open positions*, admin, ввод фразы | закрывает только позиции агента (magic), ручные не трогает |
| Остановить процесс | Ctrl+C / `docker compose stop agent` | при следующем старте агент в PAUSED, сначала сверка позиций |

## Стратегия «тренд + откат» v1.0.0

Правила записаны в docstring `trading_engine/strategy_engine.py` и выводятся в интерфейсе для каждого
решения. Кратко: тренд H1 по EMA 20/50/200 и наклону EMA50, отсутствие противоположной структуры
H1/M15, откат к EMA20 (±0.3 ATR) без глубокого пробоя EMA50, подтверждающая свеча, RSI в диапазоне и
разворачивается, MACD-гистограмма растёт, H4 не против, нормальная волатильность, спред ≤ 0.25 ATR,
торговая сессия. SL — за минимумом отката + 0.2 ATR (0.5…3 ATR), TP = 2R, ближайший уровень не
ближе 1.5R. **Прибыльность не доказана** — см. `docs/BACKTEST_REPORT.md`.

## Готовые лимиты риска (стартовые значения для демо)

0.5% на сделку, 1.5% суммарного открытого риска, 2% дневной убыток (по Equity, включая плавающий
убыток, комиссию и своп; плюс проверка «худшего случая» — если все стопы сработают), максимум
3 позиции, одна позиция на символ (сетки и усреднение невозможны), RR ≥ 1.5, торговля без SL
невозможна, повышение риска после убытка блокируется. Все значения меняются только admin'ом через
аудируемый API.

Дальше: [SECURITY.md](SECURITY.md), [TESTING.md](TESTING.md),
[docs/LIVE_CHECKLIST.md](docs/LIVE_CHECKLIST.md), [docs/BACKTEST_REPORT.md](docs/BACKTEST_REPORT.md).
