#!/usr/bin/env bash
# ============================================================================
# commit-git.sh — zet deze werkmap in één keer op GitHub
#
# Deze map is zelf de bron: er zijn geen versiemappen. Het script synct de
# inhoud naar een lokale git-clone, commit en pusht naar main, waarna de
# GitHub Action het :latest image bouwt.
#
# Gebruik:
#   ./commit-git.sh                    push met standaard commit-message
#   ./commit-git.sh "eigen bericht"    push met eigen commit-message
#   ./commit-git.sh --dry-run          laat zien wat er zou gebeuren, wijzigt niets
#   ./commit-git.sh -y                 sla de bevestiging over als GitHub commits
#                                      bevat die niet uit de werkmap komen
#                                      (die worden overschreven!)
#
# Wat het doet:
#   1. Clonet de repo als die nog niet bestaat (in $REPO_DIR)
#   2. Haalt de laatste main op en waarschuwt als GitHub verder is dan de
#      laatste push door dit script: de werkmap is de bron, GitHub wordt
#      overschreven
#   3. Synct de werkmap naar de clone — zonder state.json, token.json,
#      settings.json, back-ups en andere geheimen
#   4. Scant wat er gecommit zou worden op tokens, cookies en Telegram-
#      gegevens, en stopt als er iets in zit
#   5. Commit + push naar main  →  de Action bouwt het :latest image
#
# Daarna op de NAS: image re-pullen en de stack opnieuw deployen.
# ============================================================================
set -euo pipefail

REPO_URL="https://github.com/TerrorSource/VintedReposter.git"
REPO_DIR="${VINTEDREPOSTER_REPO_DIR:-$HOME/VintedReposter}"

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
STATE_FILE="$SRC_DIR/.commit-git-last-sha"   # sha van de laatste push door dit script

# Bestandsnamen die NOOIT naar GitHub mogen: ze bevatten je Vinted-login,
# je instellingen (met eventueel je Telegram-bot) of je advertenties.
SECRET_FILES='state\.json|token\.json|settings\.json|repost_history\.json|\.lock'
SECRET_DIRS='state|backups'

# ── Argumenten ──────────────────────────────────────────────────────────────
COMMIT_MSG="update vanuit werkmap ($(date '+%Y-%m-%d %H:%M'))"
DRY_RUN=0
ASSUME_YES=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    -y|--yes)  ASSUME_YES=1 ;;
    *)         COMMIT_MSG="$arg" ;;
  esac
done

echo "📂 Bron:        $SRC_DIR"
echo "📂 Git-clone:   $REPO_DIR"
echo "💬 Bericht:     $COMMIT_MSG"
[ "$DRY_RUN" -eq 1 ] && echo "🧪 DRY-RUN:     er wordt niets gewijzigd of gepusht"
echo ""

# ── 0. Bestaat de repo? ─────────────────────────────────────────────────────
if ! git ls-remote --heads "$REPO_URL" >/dev/null 2>&1; then
  echo "❌ $REPO_URL is niet bereikbaar. Bestaat de repo al?" >&2
  echo "   Maak hem aan op GitHub (leeg, zonder README), zet het package na de eerste" >&2
  echo "   build op public, en draai dit script opnieuw." >&2
  exit 1
fi

# ── 1. Clone indien nodig ───────────────────────────────────────────────────
if [ ! -d "$REPO_DIR/.git" ]; then
  echo "→ Geen clone gevonden, repo clonen…"
  [ "$DRY_RUN" -eq 1 ] || git clone "$REPO_URL" "$REPO_DIR"
fi
if [ "$DRY_RUN" -eq 1 ] && [ ! -d "$REPO_DIR/.git" ]; then
  echo "→ (dry-run) clone zou worden aangemaakt; verder simuleren kan niet zonder clone."
  exit 0
fi

# ── 2. Laatste main ophalen ─────────────────────────────────────────────────
echo "→ git pull (main)…"
git -C "$REPO_DIR" fetch origin
if git -C "$REPO_DIR" rev-parse --verify origin/main >/dev/null 2>&1; then
  git -C "$REPO_DIR" checkout main --quiet
  [ "$DRY_RUN" -eq 1 ] || git -C "$REPO_DIR" pull --ff-only origin main
else
  git -C "$REPO_DIR" checkout -B main --quiet     # lege repo: nieuwe main
fi

# Waarschuw als GitHub commits heeft die niet door dit script zijn gepusht
# (bijv. een gemergde Dependabot-PR). De sync hieronder draait die terug.
if [ -f "$STATE_FILE" ]; then
  LAST_SYNCED=$(cat "$STATE_FILE")
  CURRENT=$(git -C "$REPO_DIR" rev-parse HEAD)
  if [ "$LAST_SYNCED" != "$CURRENT" ] && git -C "$REPO_DIR" cat-file -e "$LAST_SYNCED" 2>/dev/null; then
    UPSTREAM=$(git -C "$REPO_DIR" log --oneline "$LAST_SYNCED..HEAD" || true)
    if [ -n "$UPSTREAM" ]; then
      echo ""
      echo "⚠️  GitHub bevat commits die niet uit de werkmap komen:"
      echo "$UPSTREAM" | sed 's/^/     /'
      echo "   Deze worden door de sync OVERSCHREVEN met de inhoud van de werkmap."
      echo "   Neem ze eerst over in de werkmap als je ze wilt behouden."
      if [ "$ASSUME_YES" -ne 1 ] && [ "$DRY_RUN" -ne 1 ]; then
        read -r -p "   Toch doorgaan? [j/N] " ANSWER
        case "$ANSWER" in
          j|J|y|Y) ;;
          *) echo "Afgebroken."; exit 1 ;;
        esac
      fi
      echo ""
    fi
  fi
fi

# ── 3. Bestanden syncen ─────────────────────────────────────────────────────
echo "→ Bestanden syncen naar de clone…"
RSYNC_FLAGS=(-a --delete)
[ "$DRY_RUN" -eq 1 ] && RSYNC_FLAGS+=(-n -v)
rsync "${RSYNC_FLAGS[@]}" \
  --exclude '.git/' \
  --exclude '.claude/' \
  --exclude '.DS_Store' \
  --exclude '__pycache__/' \
  --exclude '.commit-git-last-sha' \
  --exclude 'state.json' \
  --exclude 'token.json' \
  --exclude 'settings.json' \
  --exclude 'repost_history.json' \
  --exclude 'state/' \
  --exclude 'backups/' \
  --exclude '*.lock' \
  "$SRC_DIR/" "$REPO_DIR/"

# ── 4. Lekscan: niets dat naar jouw account of Telegram herleidt ────────────
# Kijkt naar de INHOUD van alles wat gesynct is, niet alleen naar bestandsnamen.
# JWT's (Vinted-tokens), Telegram-bottokens, datadome-cookies, chat-id's.
LEAKS=$(grep -rIn --exclude-dir=.git \
  -E 'eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}|[0-9]{8,}:[A-Za-z0-9_-]{30,}|"datadome": *"[A-Za-z0-9~_-]{40,}|TELEGRAM_CHAT_ID= *[0-9]+|TELEGRAM_BOT_TOKEN= *[0-9]' \
  "$REPO_DIR" 2>/dev/null | sed -E 's/(eyJ[A-Za-z0-9_-]{12})[A-Za-z0-9_.-]+/\1…/' | cut -c1-140 || true)
if [ -n "$LEAKS" ]; then
  echo "❌ Er staat iets in dat naar jouw account of Telegram herleidt — gestopt:" >&2
  echo "$LEAKS" | sed 's/^/     /' >&2
  echo "   Haal het uit de werkmap (of voeg het bestand toe aan de excludes) en draai opnieuw." >&2
  exit 1
fi

if [ "$DRY_RUN" -eq 1 ]; then
  echo ""
  echo "🧪 DRY-RUN klaar, lekscan schoon. Voer zonder --dry-run uit om echt te pushen."
  exit 0
fi

# OneDrive geeft bestanden op macOS een uitvoerbaar-bit; zonder normalisatie
# ziet git elke sync als 'mode change 100644 => 100755'. (De macOS-rsync kent
# --chmod niet, vandaar een aparte stap; .git blijft ongemoeid.)
find "$REPO_DIR" -path "$REPO_DIR/.git" -prune -o -type f -exec chmod 644 {} +
find "$REPO_DIR" -path "$REPO_DIR/.git" -prune -o -type d -exec chmod 755 {} +
chmod 755 "$REPO_DIR/commit-git.sh" 2>/dev/null || true

# ── 4b. Veiligheidscheck op bestandsnamen in de staging ────────────────────
cd "$REPO_DIR"
git add -A
if git diff --cached --name-only | grep -E "(^|/)($SECRET_FILES)$|(^|/)($SECRET_DIRS)/" >/dev/null; then
  echo "❌ Er zou een geheim bestand worden gecommit — gestopt om je Vinted-login te beschermen:" >&2
  git diff --cached --name-only | grep -E "(^|/)($SECRET_FILES)$|(^|/)($SECRET_DIRS)/" | sed 's/^/     /' >&2
  git reset --quiet
  exit 1
fi

# ── 5. Commit + push ────────────────────────────────────────────────────────
if git diff --cached --quiet; then
  echo "✓ Geen wijzigingen t.o.v. GitHub — niets te pushen."
else
  echo "→ Committen: \"$COMMIT_MSG\""
  git commit --quiet -m "$COMMIT_MSG"
  echo "→ Pushen naar origin/main…"
  git push origin main
  echo ""
  echo "✅ Gepusht. De GitHub Action bouwt nu het :latest image (volg het op:"
  echo "   ${REPO_URL%.git}/actions)"
fi
git rev-parse HEAD > "$STATE_FILE"

echo ""
echo "🖥  Daarna op de NAS: de stack opnieuw deployen met 'Re-pull image' aangevinkt,"
echo "   of: docker compose pull && docker compose up -d"
