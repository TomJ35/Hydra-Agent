# Hydra-Agent

> Agent de coding autonome — reçoit des tickets du Hub Hydra, code, teste et ouvre des PRs automatiquement.

Part of the **[Hydra](https://github.com/TomJ35/Hydra-Hub)** project — a distributed AI coding army running on free-tier infrastructure.

---

## Architecture

```
Hub Hydra
    │  WebSocket (ASSIGN / RESULT / LOG / HEARTBEAT)
    ▼
Agent Manager          ← loop principale, gère la connexion hub
    │
    ▼
┌─────────────────────────────────────────────────┐
│  Pour chaque ticket reçu :                      │
│                                                 │
│  1. Init                                        │
│     ├── git clone + checkout branche            │
│     ├── lecture contexte (README, arborescence) │
│     └── décomposition en sous-tâches (Gemini)   │
│                                                 │
│  2. Boucle de code (par sous-tâche)             │
│     ├── appel Gemini → génération code          │
│     ├── écriture fichiers                       │
│     ├── lancement tests (pytest / npm test)     │
│     └── retry si échec (max 3 tentatives)       │
|       └── envoi message blocage                 |
│                                                 │
│  3. Fin                                         │
│     ├── revue personnelle (Gemini)              │
│     ├── git commit + push                       │
│     ├── ouverture PR GitHub                     │
│     └── envoi RESULT au hub                     │
│                                                 │
│  4. Correction (si remarque PR)                 │
│     └── retour boucle Code                      │
└─────────────────────────────────────────────────┘
```

---

## Stack

| Composant | Technologie |
|---|---|
| Langage | Python 3.11 |
| Communication hub | WebSocket (`websockets`) |
| LLM primaire | Gemini 2.5 Flash (Google AI) |
| LLM fallback | Gemini 2.5 Flash (projet GCP secondaire) |
| Git | `gitpython` |
| GitHub API | `httpx` |
| Tests détectés | `pytest` · `npm test` · `cargo test` |
| Containerisation | Docker |
| Hébergement | Render free tier |

## Messages WebSocket

L'agent communique avec le hub via des messages JSON typés.

### Reçus depuis le hub

```json
// ASSIGN — nouveau ticket à traiter
{
  "type": "assign",
  "ticket": {
    "id": "TICK-A3F2B1C0",
    "title": "Ajouter validation email",
    "description": "...",
    "repo_url": "https://github.com/org/repo",
    "base_branch": "main",
    "files_hint": ["src/forms/register.py"],
    "acceptance_criteria": ["email invalide → 400"]
  },
  "git_token": "ghp_...",
  "ts": "2025-05-20T14:00:00Z"
}
```

### Envoyés au hub

```json
// HEARTBEAT — toutes les X secondes
{
  "type": "heartbeat",
  "agent_id": "agent-01",
  "status": "idle",
  "ts": "2025-05-20T14:00:30Z"
}

// LOG — streaming en temps réel
{
  "type": "log",
  "agent_id": "agent-01",
  "ticket_id": "TICK-A3F2B1C0",
  "level": "info",
  "message": "git clone terminé · 2847 fichiers",
  "ts": "2025-05-20T14:00:35Z"
}

// RESULT — ticket terminé
{
  "type": "result",
  "agent_id": "agent-01",
  "ticket_id": "TICK-A3F2B1C0",
  "status": "done",
  "pr_url": "https://github.com/org/repo/pull/47",
  "attempts": 2,
  "duration_s": 312,
  "ts": "2025-05-20T14:05:12Z"
}

// ERROR — blocage, intervention requise
{
  "type": "error",
  "agent_id": "agent-01",
  "ticket_id": "TICK-A3F2B1C0",
  "step": "test",
  "error_msg": "AssertionError: expected 400 got 200",
  "retry_count": 3,
  "ts": "2025-05-20T14:08:00Z"
}
```

---

## Variables d'environnement

```env
# Hub
HUB_URL=wss://hub-hydra.onrender.com/ws/agent
AGENT_ID=agent-01                        # unique par instance

# LLM
GEMINI_API_KEY=Abcd...                   # clé projet GCP dédié
GEMINI_MODEL=gemini-2.5-flash

# GitHub
GH_TOKEN=ghp_...                         # token avec droits repo + PR

# Config agent
MAX_RETRIES=3                            # tentatives max par sous-tâche
WORKSPACE_DIR=/tmp/hydra                 # repos clonés temporairement
HEARTBEAT_INTERVAL=30                    # secondes
```

---

## Déploiement Render

Pour déployer un second agent depuis le même repo :
1. Créer un nouveau service Render pointant sur ce repo
2. Changer `AGENT_ID=agent-02`
3. Utiliser une `GEMINI_API_KEY` d'un **projet GCP différent** (quotas séparés)

---

## Limites et considérations

| Limite | Valeur | Impact |
|---|---|---|
| Gemini 2.5 Flash free | 1 500 req/jour · 15 RPM | ~375 tickets/jour par agent |
| Render RAM | 512 MB | Tickets niveau 1-2 recommandés |
| Render sleep | 15 min inactivité | Hub envoie keepalive automatique |
| Workspace /tmp | Nettoyé après chaque ticket | Pas d'accumulation disque |
| Max retries | 3 par sous-tâche | Ticket → `failed` si dépassé |

---