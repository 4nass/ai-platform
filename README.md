# AI Software Engineering Platform

## Vision

AI Software Engineering Platform est une plateforme d'orchestration d'agents IA destinée à automatiser et accélérer le cycle complet de développement logiciel.

L'objectif n'est pas de remplacer les développeurs, mais de créer une équipe virtuelle d'agents spécialisés capables de collaborer sur un projet logiciel :

* analyse du besoin,
* conception d'architecture,
* génération de code,
* revue technique,
* tests,
* sécurité,
* documentation,
* déploiement.

La plateforme agit comme un **Engineering Operating System** permettant de coordonner plusieurs modèles IA (Claude, Codex, modèles locaux, etc.) tout en optimisant l'utilisation du contexte et des tokens.

---

# Objectifs du projet

## 1. Créer une équipe de développement IA autonome

Le système doit permettre de déléguer des tâches complexes à plusieurs agents spécialisés.

Exemple :

> "Ajouter une authentification OAuth2 avec Microsoft Entra ID"

La plateforme doit automatiquement :

1. analyser la demande,
2. identifier les composants impactés,
3. créer un plan d'exécution,
4. assigner les tâches aux bons agents,
5. générer les modifications,
6. exécuter les tests,
7. effectuer une revue sécurité,
8. mettre à jour la documentation.

---

## 2. Optimiser l'utilisation des modèles IA

Le principal problème des assistants actuels est la gestion du contexte.

Envoyer un projet complet à un LLM :

* augmente les coûts,
* réduit la qualité,
* dépasse rapidement les limites de contexte.

La plateforme introduit un **Context Engineering Layer** capable de fournir uniquement les informations nécessaires.

Exemple :

Au lieu d'envoyer :

```
5000 fichiers
500 000 lignes
```

le système sélectionne :

```
AuthController.java
JwtService.java
SecurityConfig.java
architecture.md
```

uniquement les éléments nécessaires à la tâche.

---

# Architecture globale

```text
                         User
                          |
                          |
                          v

                  Hermes Orchestrator

                          |
        +-----------------+-----------------+
        |                 |                 |
        v                 v                 v

   Task Planner      Scheduler        Supervisor


                          |
                          v

                Agent Execution Layer

        +-------------+-------------+-------------+
        |             |             |             |
        v             v             v             v

   Architect      Backend       DevOps       Security
   Agent          Agent        Agent        Agent


                          |
                          v

              Context Engineering Layer

        +-------------+-------------+-------------+
        |             |             |             |
        v             v             v             v

     Git Analysis  Code Graph     RAG        Memory


                          |
                          v

                    LLM Providers

        +-------------+-------------+-------------+
        |             |             |             |
        v             v             v             v

      Claude        Codex       Local Models   Others
```

---

# Composants principaux

# 1. Hermes Orchestrator

## Rôle

Hermes est le cerveau d'orchestration.

Il ne produit pas directement du code.

Ses responsabilités :

* comprendre la demande utilisateur,
* découper le travail,
* créer un workflow,
* choisir les agents,
* gérer les dépendances,
* contrôler l'exécution,
* superviser les résultats.

Architecture interne :

```
Hermes

├── Planner
│
├── Scheduler
│
├── Supervisor
│
└── Workflow Engine
```

---

# 2. Agent Layer

Chaque agent possède une spécialisation.

## Architect Agent

Responsabilités :

* analyse technique,
* choix d'architecture,
* création de diagrammes,
* décisions techniques.

---

## Backend Agent

Responsabilités :

* développement API,
* logique métier,
* base de données,
* intégration backend.

---

## Frontend Agent

Responsabilités :

* interfaces utilisateur,
* composants UI,
* intégration API.

---

## DevOps Agent

Responsabilités :

* Docker/Podman,
* Kubernetes,
* CI/CD,
* infrastructure as code.

---

## Security Agent

Responsabilités :

* analyse vulnérabilités,
* OWASP,
* secrets,
* conformité.

---

## Documentation Agent

Responsabilités :

* README,
* documentation API,
* architecture,
* ADR.

---

# 3. Context Engineering Layer

Cette couche est la partie critique du système.

Elle décide quelles informations doivent être envoyées aux agents.

Architecture :

```
User Request

      |
      v

Context Manager

      |
      +---- Git Diff
      |
      +---- Code Graph
      |
      +---- Vector Search
      |
      +---- Project Memory
      |
      +---- Documentation

      |
      v

Optimized Context

      |
      v

LLM Agent
```

---

# 4. Code Intelligence Engine

Cette couche comprend le projet.

Elle utilise :

* Tree-sitter,
* analyse AST,
* Git history,
* graphes de dépendances.

Objectifs :

* comprendre les relations entre fichiers,
* identifier les impacts d'une modification,
* éviter d'envoyer du code inutile aux modèles.

Exemple :

Modification :

```
JwtService.java
```

Le système détecte :

```
JwtService

 |
 +-- AuthController

 |
 +-- SecurityConfig

 |
 +-- TokenRepository
```

---

# 5. Memory System

La mémoire conserve la connaissance du projet.

Structure :

```
memory/

├── architecture.md

├── coding_rules.md

├── business_rules.md

├── roadmap.md

├── decisions/

│   ├── ADR-001.md
│   └── ADR-002.md

└── glossary.md
```

Types de mémoire :

## Mémoire technique

Exemple :

* architecture choisie,
* frameworks utilisés,
* conventions.

## Mémoire métier

Exemple :

* règles fonctionnelles,
* contraintes métier.

## Mémoire décisionnelle

Architecture Decision Records :

Pourquoi une décision a été prise.

---

# 6. Vector Database / RAG

La base vectorielle permet une recherche sémantique dans le projet.

Exemple :

Question :

```
Où est gérée l'authentification ?
```

Recherche :

```
AuthenticationService
JWTProvider
SecurityConfig
OAuthController
```

Technologies possibles :

* Qdrant,
* PostgreSQL + pgvector,
* LanceDB.

---

# 7. MCP Integration Layer

Model Context Protocol permet aux agents d'accéder aux outils externes.

Exemples :

```
Agent

 |

 MCP

 |

+-------------+
| GitHub      |
| Git         |
| Podman      |
| Kubernetes  |
| Database    |
| Cloud       |
+-------------+
```

Les agents peuvent :

* créer une branche Git,
* analyser un dépôt,
* lancer des tests,
* créer une Pull Request,
* interagir avec l'infrastructure.

---

# Gestion du workflow

Exemple :

Demande :

```
Créer une API utilisateur avec authentification
```

Workflow généré :

```
Planner

 |
 +-- Architecture
 |
 +-- Backend
 |
 +-- Tests
 |
 +-- Security Review
 |
 +-- Documentation


Scheduler

 |
 +-- Claude → Architecture
 |
 +-- Codex → Backend
 |
 +-- Codex → Tests
 |
 +-- Claude → Security
 |
 +-- Claude → Documentation


Supervisor

 |
 +-- Validation
 |
 +-- Correction
 |
 +-- Merge
```

---

# Optimisation des tokens

Le système applique plusieurs stratégies :

## 1. Sélection intelligente du contexte

Ne jamais envoyer un projet complet.

---

## 2. Recherche avant génération

Avant d'appeler un LLM :

```
Question

↓

Recherche

↓

Contexte minimal

↓

LLM
```

---

## 3. Agents spécialisés

Un agent reçoit uniquement les informations nécessaires à son rôle.

---

## 4. Cache

Les résultats fréquents sont conservés.

---

## 5. Budget par agent

Exemple :

```yaml
architect:
  max_tokens: 12000

backend:
  max_tokens: 10000

reviewer:
  max_tokens: 8000
```

---

# Infrastructure cible

Environnement recommandé :

```
Windows

 |
 +-- WSL2 Ubuntu

       |
       +-- Python
       +-- uv
       +-- Claude Code
       +-- Codex CLI
       +-- Podman Engine
       +-- Qdrant
       +-- Redis
       +-- PostgreSQL
       +-- Hermes
```

Podman Desktop peut être utilisé comme interface graphique connectée au moteur Podman exécuté dans WSL2.

---

# Roadmap

## Phase 1 - Foundation

* [ ] Structure du projet
* [ ] Configuration Python
* [ ] Gestion des modèles IA
* [ ] CLI

---

## Phase 2 - Context Engine

* [ ] Analyse Git
* [ ] Parsing code avec Tree-sitter
* [ ] Graphe de dépendances
* [ ] Recherche vectorielle
* [ ] Memory Manager

---

## Phase 3 - Agent System

* [ ] Architect Agent
* [ ] Developer Agent
* [ ] Reviewer Agent
* [ ] Security Agent
* [ ] Documentation Agent

---

## Phase 4 - Hermes

* [ ] Planner
* [ ] Scheduler
* [ ] Supervisor
* [ ] Workflow Engine

---

## Phase 5 - Automation

* [ ] MCP
* [ ] CI/CD
* [ ] Pull Request automatique
* [ ] Tests automatiques
* [ ] Security scanning

---

# Principes du projet

## Modularité

Chaque composant doit être remplaçable.

Exemple :

Changer Claude par un modèle local ne doit pas nécessiter de réécrire le système.

---

## Context First

La qualité d'un agent dépend principalement de la qualité du contexte fourni.

---

## Human in the Loop

Les décisions critiques restent validées par un humain.

---

## Security by Design

Chaque génération de code doit pouvoir être analysée et contrôlée.

---

# Vision finale

Construire une plateforme capable de transformer une idée en logiciel fonctionnel en orchestrant une équipe complète d'agents IA, tout en gardant :

* contrôle du code,
* maîtrise des coûts,
* traçabilité des décisions,
* sécurité,
* qualité logicielle.
