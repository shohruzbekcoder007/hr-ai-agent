# HR AI Agent — Local Installation Guide

Bu qo‘llanma loyihani **o‘z kompyuteringizda** (Windows / macOS / Linux) ishga tushirish uchun.

Production (Proxmox + Ubuntu VM) uchun: **[install.md](install.md)**  
Arxitektura va API: **[README.md](README.md)**

---

## Mazmun

1. [Local nima degani?](#1-local-nima-degani)
2. [Talablar](#2-talablar)
3. [Windows (Docker Desktop) — tavsiya](#3-windows-docker-desktop--tavsiya)
4. [macOS (Docker Desktop)](#4-macos-docker-desktop)
5. [Linux (Docker Engine)](#5-linux-docker-engine)
6. [Environment (.env) sozlash](#6-environment-env-sozlash)
7. [Build va ishga tushirish](#7-build-va-ishga-tushirish)
8. [Tekshirish](#8-tekshirish)
9. [Agent bilan suhbat](#9-agent-bilan-suhbat)
10. [Loglar](#10-loglar)
11. [To‘xtatish / qayta ishga tushirish](#11-toxtatish--qayta-ishga-tushirish)
12. [employees.json ni o‘zgartirish](#12-employeesjson-ni-ozgartirish)
13. [Ollama (lokal model, ixtiyoriy)](#13-ollama-lokal-model-ixtiyoriy)
14. [Docker’siz qisman test (ixtiyoriy)](#14-dockersiz-qisman-test-ixtiyoriy)
15. [Muammolarni tuzatish](#15-muammolarni-tuzatish)
16. [Tezkor cheat-sheet](#16-tezkor-cheat-sheet)

---

## 1. Local nima degani?

Local = loyiha **o‘z PC** da ishlaydi:

```text
Sizning kompyuter
      │
      ▼
 Docker Desktop / Docker Engine
      │
      ▼
 Bitta container: hr-ai-agent
      ├─ Hermes
      ├─ HR Agent
      ├─ employees.json
      └─ API → http://127.0.0.1:8080
```

| | Local | Production (install.md) |
|--|--------|-------------------------|
| Qayerda | O‘z PC | Proxmox VM |
| URL | `http://127.0.0.1:8080` | `http://<vm-ip>:8080` |
| Kod | Bir xil | Bir xil |
| Maqsad | Dev / test / o‘rganish | 24/7 server |

**Bitta container** ichida Hermes + HR Agent birga ishlaydi (alohida Hermes container yo‘q).

---

## 2. Talablar

| Component | Minimal | Tavsiya |
|-----------|---------|---------|
| OS | Windows 10/11, macOS 12+, Ubuntu 22.04+ | — |
| RAM | 8 GB | 16 GB |
| Disk | ~10 GB bo‘sh joy (image + Hermes build) | 20 GB+ |
| Docker | Docker Desktop yoki Docker Engine + Compose v2 | So‘nggi stable |
| Internet | Build va (odatda) LLM uchun | Ha |
| LLM | OpenRouter **yoki** OpenAI **yoki** Anthropic **yoki** Ollama | OpenRouter qulay |

### Windows uchun qo‘shimcha

- [Docker Desktop for Windows](https://www.docker.com/products/docker-desktop/)
- WSL2 yoqilgan bo‘lishi kerak (Docker Desktop odatda so‘raydi)
- PowerShell yoki Windows Terminal

### LLM kalit

Chat ishlashi uchun kamida **bitta** yo‘l:

1. [OpenRouter](https://openrouter.ai) → API Keys  
2. yoki [OpenAI](https://platform.openai.com)  
3. yoki [Anthropic](https://console.anthropic.com)  
4. yoki local **Ollama** (internet keyin shart emas, model oldin yuklanadi)

> **Eslatma:** OpenRouter — local kutubxona emas, cloud xizmat. Internetsiz ishlamaydi.  
> To‘liq offline uchun [Ollama bo‘limiga](#13-ollama-lokal-model-ixtiyoriy) qarang.

---

## 3. Windows (Docker Desktop) — tavsiya

### 3.1 Docker Desktop o‘rnatish

1. https://www.docker.com/products/docker-desktop/ dan yuklab oling  
2. Installer ni ishga tushiring  
3. Kerak bo‘lsa **Use WSL 2** ni tanlang  
4. Kompyuterni restart qiling (so‘ralsa)  
5. Docker Desktop ni oching va “Engine running” holatini kuting  

Tekshirish (PowerShell):

```powershell
docker version
docker compose version
```

Ikkala buyruq ham versiya chiqarsa — tayyor.

### 3.2 Loyiha papkasiga o‘tish

Loyiha allaqachon bor bo‘lsa:

```powershell
cd D:\GROK\hermes_test_1
```

Git orqali olinsa:

```powershell
cd D:\GROK
git clone <SIZNING_REPO_URL> hermes_test_1
cd hermes_test_1
```

Papka ichida bo‘lishi kerak:

```text
Dockerfile
docker-compose.yml
.env.example
data\employees.json
agents\
tools\
...
```

### 3.3 .env yaratish

```powershell
copy .env.example .env
notepad .env
```

Keyin [6-bo‘lim](#6-environment-env-sozlash) dagi kalitlarni to‘ldiring, saqlang.

### 3.4 Build va start

```powershell
docker compose build
docker compose up -d
docker compose ps
docker compose logs -f
```

Birinchi `build` **5–20 daqiqa** olishi mumkin (Hermes GitHub’dan o‘rnatiladi). Keyingi buildlar tezroq.

`Ctrl+C` log follow ni to‘xtatadi (container ishlashda qoladi).

---

## 4. macOS (Docker Desktop)

```bash
# Docker Desktop o‘rnating: https://www.docker.com/products/docker-desktop/

cd /path/to/hermes_test_1
cp .env.example .env
nano .env   # API key ni yozing

docker compose build
docker compose up -d
docker compose logs -f
```

Tekshirish:

```bash
curl -sS http://127.0.0.1:8080/ready
```

---

## 5. Linux (Docker Engine)

Ubuntu misoli:

```bash
# Docker Engine + Compose plugin (qisqa)
sudo apt update
sudo apt -y install ca-certificates curl
# Rasmiy Docker repo o‘rnatish: https://docs.docker.com/engine/install/ubuntu/

sudo usermod -aG docker "$USER"
# logout / login

cd /path/to/hermes_test_1
cp .env.example .env
nano .env

docker compose build
docker compose up -d
```

Batafsil server o‘rnatish: [install.md](install.md).

---

## 6. Environment (.env) sozlash

`.env` loyiha ildizida bo‘lishi kerak (Compose uni o‘qiydi).

### Minimal — OpenRouter

```env
APP_ENV=development
APP_PORT=8080
LOG_LEVEL=DEBUG

OPENROUTER_API_KEY=sk-or-v1-XXXXXXXX
HR_MODEL=anthropic/claude-sonnet-4.6

# Local da odatda token shart emas
API_BEARER_TOKEN=
```

### Minimal — OpenAI

```env
OPENAI_API_KEY=sk-XXXXXXXX
HR_MODEL=gpt-4.1
```

### Minimal — Anthropic

```env
ANTHROPIC_API_KEY=sk-ant-XXXXXXXX
HR_MODEL=claude-sonnet-4-6
```

### Local uchun qulay sozlamalar

```env
APP_ENV=development
LOG_LEVEL=DEBUG
LOG_FORMAT=plain
CORS_ORIGINS=*
API_BEARER_TOKEN=
```

> Container ichidagi yo‘llar (`EMPLOYEES_JSON_PATH=/app/data/...`) ni o‘zgartirmang — ular image uchun.  
> Hostdagi fayllar `docker-compose.yml` volume orqali ulanadi (`./data`, `./logs`, `./prompts`).

---

## 7. Build va ishga tushirish

Loyiha ildizidan:

```bash
# Windows PowerShell / macOS / Linux — bir xil buyruqlar
docker compose build
docker compose up -d
```

### Hermes versiyasini pin qilish (ixtiyoriy)

```bash
docker compose build --build-arg HERMES_REF=main
# yoki tag:
docker compose build --build-arg HERMES_REF=v0.18.2
```

### Toza qayta build

```bash
docker compose build --no-cache
docker compose up -d --force-recreate
```

---

## 8. Tekshirish

### Container holati

```bash
docker compose ps
```

Kutilgan: `hr-ai-agent` → `Up` va birozdan keyin `(healthy)`.

### Health

**PowerShell:**

```powershell
curl.exe -sS http://127.0.0.1:8080/health
curl.exe -sS http://127.0.0.1:8080/ready
curl.exe -sS http://127.0.0.1:8080/v1/info
```

**bash / macOS / Linux / Git Bash:**

```bash
curl -sS http://127.0.0.1:8080/health | jq .
curl -sS http://127.0.0.1:8080/ready | jq .
curl -sS http://127.0.0.1:8080/v1/info | jq .
```

Brauzer:

- Health: http://127.0.0.1:8080/health  
- OpenAPI: http://127.0.0.1:8080/docs  

### Faqat JSON / tool (LLM shartsiz)

```bash
curl -sS http://127.0.0.1:8080/v1/tools/count_employees \
  -H "Content-Type: application/json" \
  -d "{\"arguments\":{}}"
```

Bu endpoint LLM chaqirmaydi — API key bo‘lmasa ham ishlashi kerak.

---

## 9. Agent bilan suhbat

### Brauzer (eng oson)

1. Ochish: http://127.0.0.1:8080/docs  
2. `POST /v1/chat` → **Try it out**  
3. Body:

```json
{
  "message": "How many employees work here?"
}
```

4. **Execute**

### curl — PowerShell

```powershell
curl.exe -sS http://127.0.0.1:8080/v1/chat `
  -H "Content-Type: application/json" `
  -d "{\"message\":\"How many employees work here?\"}"
```

Boshqa savollar:

```powershell
curl.exe -sS http://127.0.0.1:8080/v1/chat `
  -H "Content-Type: application/json" `
  -d "{\"message\":\"Find all employees with Python skill. Return a markdown table.\"}"

curl.exe -sS http://127.0.0.1:8080/v1/chat `
  -H "Content-Type: application/json" `
  -d "{\"message\":\"Who has the highest salary?\"}"

curl.exe -sS http://127.0.0.1:8080/v1/chat `
  -H "Content-Type: application/json" `
  -d "{\"message\":\"List everyone in the HR department.\"}"
```

### curl — bash

```bash
curl -sS http://127.0.0.1:8080/v1/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"How many employees work here?"}' | jq .
```

### Multi-turn session

```bash
curl -sS http://127.0.0.1:8080/v1/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"Who is EMP-005?","session_id":"local-1"}'

curl -sS http://127.0.0.1:8080/v1/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"What is their department and salary?","session_id":"local-1"}'
```

> Session tarixi **RAM** da; `docker compose restart` dan keyin tozalanadi.

### Bearer token yoqilgan bo‘lsa

`.env`:

```env
API_BEARER_TOKEN=my-secret-local-token
```

So‘rov:

```bash
curl -sS http://127.0.0.1:8080/v1/chat \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer my-secret-local-token' \
  -d '{"message":"Count departments"}'
```

---

## 10. Loglar

### Container loglari

```bash
docker compose logs -f hr-agent
docker compose logs --tail=100 hr-agent
```

### Hostdagi fayllar

Loyiha papkasida:

```text
logs/hr-agent.log
logs/hr-agent-errors.log
```

**Windows:**

```powershell
Get-Content .\logs\hr-agent.log -Wait -Tail 50
```

**Linux/macOS:**

```bash
tail -f logs/hr-agent.log
```

---

## 11. To‘xtatish / qayta ishga tushirish

```bash
# Qayta ishga tushirish
docker compose restart

# To‘xtatish (container qoladi)
docker compose stop

# Yana start
docker compose start

# To‘xtatish va container olib tashlash (image qoladi)
docker compose down

# Volume bilan tozalash (Hermes home ham o‘chadi)
docker compose down -v
```

Kod o‘zgarganda:

```bash
docker compose build
docker compose up -d
```

Faqat `data/`, `prompts/`, `config/` o‘zgarganda (volume):

```bash
docker compose restart
```

---

## 12. employees.json ni o‘zgartirish

Hostda tahrirlang:

```text
data/employees.json
```

JSON ni tekshirish:

```bash
# Python bor bo‘lsa
python -m json.tool data/employees.json > NUL     # Windows
python3 -m json.tool data/employees.json > /dev/null  # Linux/macOS
```

Hot-reload (container ishlab tursa):

```bash
curl -sS http://127.0.0.1:8080/v1/tools/reload_employees \
  -H "Content-Type: application/json" \
  -d "{\"arguments\":{}}"
```

Yoki:

```bash
docker compose restart
```

Bilim manbai faqat shu JSON — DB yo‘q.

---

## 13. Ollama (lokal model, ixtiyoriy)

OpenRouter/OpenAI o‘rniga **o‘z kompingizdagi model**:

### 13.1 Ollama o‘rnatish

- Windows/macOS: https://ollama.com/download  
- Model yuklash:

```bash
ollama pull llama3.1
ollama list
```

### 13.2 .env

```env
OPENAI_API_KEY=ollama
OPENAI_BASE_URL=http://host.docker.internal:11434/v1
HR_MODEL=llama3.1

# OpenRouter bo'sh
OPENROUTER_API_KEY=
```

> `host.docker.internal` — container ichidan host dagi Ollama ga chiqish (Docker Desktop da odatda ishlaydi).

Linux da ba’zan:

```env
OPENAI_BASE_URL=http://172.17.0.1:11434/v1
```

yoki Compose da `extra_hosts` / `network_mode` sozlash kerak bo‘ladi.

### 13.3 Qayta ishga tushirish

```bash
docker compose up -d --force-recreate
curl -sS http://127.0.0.1:8080/v1/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"How many employees?"}'
```

**Eslatma:** Kichik local modellar tool-calling da cloud modellarga qaraganda zaifroq bo‘lishi mumkin. Muammo bo‘lsa, avval tool endpoint bilan JSON ni tekshiring, keyin modelni almashtiring.

---

## 14. Docker’siz qisman test (ixtiyoriy)

To‘liq agent (Hermes + chat) uchun Docker tavsiya etiladi.  
Faqat **employees.json / service** ni tekshirish:

```bash
# Loyiha ildizidan
python scripts/validate_data.py
```

Kutilgan:

```text
OK — employees: 25
OK — departments: 8
...
```

Bu LLM va Docker talab qilmaydi.

Hermes’ni hostga to‘g‘ridan-to‘g‘ri o‘rnatish mumkin, lekin Windows da qiyinroq; local dev uchun **Docker Desktop** eng barqaror yo‘l.

---

## 15. Muammolarni tuzatish

### Docker ishlamayapti

```text
error during connect: ... dockerDesktopLinuxEngine
```

- Docker Desktop ni oching  
- “Engine running” bo‘lishini kuting  
- WSL2 integratsiyasini tekshiring  

### Port 8080 band

```text
Bind for 0.0.0.0:8080 failed: port is already allocated
```

`.env` da:

```env
APP_PORT=8081
```

```bash
docker compose up -d
# endi: http://127.0.0.1:8081
```

### Build xatosi / tarmoq

- Internetni tekshiring (GitHub + PyPI)  
- VPN bo‘lsa o‘chirib qayta urinib ko‘ring  
- `docker compose build --no-cache`

### `/ready` 503

```bash
docker compose logs --tail=200 hr-agent
```

- `employees.json` bormi: `data\employees.json`  
- Volume ruxsatlari  
- Container to‘liq start bo‘lganini kuting (birinchi start 30–60 s)

### Chat xato, tool ishlaydi

- `.env` da API key bormi?  
- `docker compose up -d --force-recreate` (env o‘zgarishi uchun)  
- Model nomi to‘g‘rimi (`HR_MODEL`)?  
- OpenRouter balans / limit?

```bash
docker compose exec hr-agent sh -c 'env | grep -E "API_KEY|HR_MODEL|BASE_URL" | sed "s/=.*/=***/"'
```

### Windows `curl` JSON muammosi

PowerShell da `curl` ba’zan alias. Ishlatilsin:

```powershell
curl.exe -sS ...
```

yoki brauzer: http://127.0.0.1:8080/docs

### logs papkasi bo‘sh / ruxsat

```powershell
# Windows
New-Item -ItemType Directory -Force -Path logs | Out-Null
```

```bash
mkdir -p logs
```

### Container healthy emas

```bash
docker inspect --format="{{json .State.Health}}" hr-ai-agent
docker compose exec hr-agent /app/scripts/healthcheck.sh
```

---

## 16. Tezkor cheat-sheet

```powershell
# === Windows PowerShell ===
cd D:\GROK\hermes_test_1
copy .env.example .env
notepad .env
# OPENROUTER_API_KEY=... ni yozing

docker compose build
docker compose up -d
docker compose logs -f

curl.exe -sS http://127.0.0.1:8080/ready
curl.exe -sS http://127.0.0.1:8080/v1/chat -H "Content-Type: application/json" -d "{\"message\":\"How many employees work here?\"}"

# Brauzer
start http://127.0.0.1:8080/docs

docker compose restart
docker compose down
```

```bash
# === macOS / Linux ===
cd /path/to/hermes_test_1
cp .env.example .env
nano .env

docker compose build && docker compose up -d
curl -sS http://127.0.0.1:8080/ready | jq .
curl -sS http://127.0.0.1:8080/v1/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"How many employees work here?"}' | jq .
```

---

## Keyingi qadamlar

| Maqsad | Hujjat |
|--------|--------|
| Production / Proxmox / Ubuntu VM | [install.md](install.md) |
| Arxitektura, API, papkalar | [README.md](README.md) |
| Env o‘zgaruvchilar ro‘yxati | [.env.example](.env.example) |

---

## Xulosa

1. Docker Desktop (yoki Engine) o‘rnating.  
2. `.env` yarating va LLM kalitini yozing.  
3. `docker compose build && docker compose up -d`.  
4. http://127.0.0.1:8080/docs orqali suhbatlashing.  
5. Bilim faqat `data/employees.json` da — uni tahrirlab reload qiling.

Savol qolsa: avval `docker compose logs -f hr-agent` ni oching — deyarli barcha local muammolar shu yerda ko‘rinadi.
