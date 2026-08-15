# Remote security readiness gate

Issue #49 ajoute une barrière explicite avant toute exposition de l API REST/SSE à un réseau distant. Le moteur ne considère pas une API fonctionnelle comme une API exploitable : chaque frontière de confiance doit produire une preuve locale.

## Commande

    ai-platform security-check
    ai-platform security-check --json

La commande retourne 0 seulement lorsque la décision est GO ou RISK_ACCEPTED. Elle retourne 1 pour NO_GO. Le JSON est versionné (version v1) afin de pouvoir être archivé dans une CI ou attaché à une release. Les valeurs secrètes ne sont jamais imprimées.

## Décisions

- GO : tous les contrôles bloquants sont PASS.
- NO_GO : au moins un contrôle bloquant est FAIL; le service doit rester local ou désactivé.
- RISK_ACCEPTED : un responsable a enregistré une exception temporaire; remote_ready reste false et l exception est visible dans le rapport. Cette décision ne supprime pas les contrôles et doit être revue avant expiration.

Les statuts WARN sont informatifs et ne permettent pas de contourner un FAIL.

## Matrice de preuves

| Frontière | Preuve contrôlée | Échec typique |
| --- | --- | --- |
| Identité | identifiants de transport et scopes jobs:submit/read/cancel/approve | credentials absents ou incomplets |
| Projet | config/projects.yaml, racines canoniques et actions allowlistées | projet inconnu, chemin absent ou action non déclarée |
| Exposition | bind non-loopback explicite, TLS terminé, rate limit activé | écoute distante implicite |
| Rejeu | contrat d authentification et store de replay du transport | nonce/requête réutilisable |
| Budget | mode strict, classes déclarées, plafonds temps/coût | budget soft ou plafond non appliqué (#45) |
| Actions | approbations et exécuteur audité | dispatch hors politique |
| Sandbox | Bubblewrap et test_sandbox: true dans la configuration commitée | test non isolé ou bwrap absent |
| Secrets | primitives de redaction + politique de rétention explicite (#35) | secret dans logs, événements, artefacts ou notifications |
| API | endpoints REST/SSE et scopes authentifiés de #47 | route ou scope manquant |
| Audit | événements jobs et télémétrie durables | action non traçable |

Le rapport vérifie les primitives disponibles; la revue de release doit conserver le JSON et compléter la vérification des sinks (logs, artefacts et notifications) avec un test d injection de secret.

## Politique réseau et arrêt d urgence

Le serveur accepte toujours 127.0.0.1, ::1 et localhost. Tout bind non-loopback est refusé sauf si les trois variables suivantes valent explicitement true (ou 1/yes/on) :

    AI_PLATFORM_REMOTE_ENABLED=true
    AI_PLATFORM_TLS_TERMINATED=true
    AI_PLATFORM_RATE_LIMIT=true

Pour couper immédiatement l exposition :

    AI_PLATFORM_REMOTE_ENABLED=false
    # puis redémarrer le managed local user service

Les credentials doivent être injectés via AI_PLATFORM_TRANSPORT_CREDENTIALS depuis le gestionnaire de secrets du service; ils ne doivent pas être commités.

## Acceptation de risque

Une exception volontaire est un JSON local non versionné par défaut (config/security-risk-acceptance.json, ou chemin indiqué par AI_PLATFORM_RISK_ACCEPTANCE_FILE) :

    {
      "id": "RA-49-001",
      "owner": "security-owner",
      "scope": "remote-mvp",
      "expires_at": "2026-09-01T00:00:00+00:00",
      "rationale": "Exception temporaire et revue planifiée."
    }

Le fichier doit contenir un propriétaire, une justification, le scope exact remote-mvp et une date future avec fuseau. Il ne transforme pas remote_ready en true; il autorise uniquement une décision opérateur explicitement nommée.

## État MVP

Le gate est livré mais le dépôt reste volontairement NO_GO tant que les dépendances suivantes ne sont pas closes : plafonds temps/USD réellement appliqués (#45), rétention/redaction complète (#35), sandbox disponible sur l hôte et configuration de production (credentials, TLS, rate limiting).
