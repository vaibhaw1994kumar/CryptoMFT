# testnet_keys.sh - keep exchange TESTNET keys in the macOS Keychain and load them as environment variables.
#
# Variable names are the ones ollog reads (ollog/endpoints.py):
#   Bybit testnet (one pair for spot and linear): BYBIT_TESTNET_KEY, BYBIT_TESTNET_SECRET
#   Binance USD-M futures testnet:                 BINANCE_FUT_TESTNET_KEY, BINANCE_FUT_TESTNET_SECRET
#   Binance spot testnet (optional):               BINANCE_SPOT_TESTNET_KEY, BINANCE_SPOT_TESTNET_SECRET
#
# Lists are written out literally so the script works the same when sourced in zsh (the macOS default) or bash.
#
# Usage (bash or zsh):
#   sh testnet_keys.sh set            store or replace the keys (hidden prompts; nothing is echoed or saved in history)
#   sh testnet_keys.sh setclip        same, but reads each value from the clipboard (for terminals where paste fails)
#   sh testnet_keys.sh check          show which keys are stored and which are loaded (lengths only, never values)
#   . ./testnet_keys.sh load          export the stored keys into THIS shell (must be sourced with ". " or "source")
#   . ./testnet_keys.sh unload        remove them from this shell
#   sh testnet_keys.sh delete         remove all of them from the Keychain
#
# TESTNET keys only. Never put mainnet keys here, never paste a key into a chat, a file or a command line.

_tk_service="cryptomft-testnet"
_tk_required="BYBIT_TESTNET_KEY BYBIT_TESTNET_SECRET BINANCE_FUT_TESTNET_KEY BINANCE_FUT_TESTNET_SECRET"
_tk_optional="BINANCE_SPOT_TESTNET_KEY BINANCE_SPOT_TESTNET_SECRET"

_tk_get() { security find-generic-password -s "$_tk_service" -a "$1" -w 2>/dev/null; }

_tk_set() {
  for v in BYBIT_TESTNET_KEY BYBIT_TESTNET_SECRET BINANCE_FUT_TESTNET_KEY BINANCE_FUT_TESTNET_SECRET BINANCE_SPOT_TESTNET_KEY BINANCE_SPOT_TESTNET_SECRET; do
    case " $_tk_optional " in *" $v "*) _opt=" (optional, press Enter to skip)";; *) _opt="";; esac
    printf '%s%s: ' "$v" "$_opt"
    stty -echo 2>/dev/null; IFS= read -r _val; stty echo 2>/dev/null; printf '\n'
    if [ -z "$_val" ]; then
      [ -n "$_opt" ] && continue
      echo "  empty, skipped; $v will be missing"; continue
    fi
    case "$_val" in *" "*) echo "  contains a space, skipped (check the copy)"; _val=""; continue;; esac
    # -U replaces an existing item. The value is stored only in your login Keychain, never in a file or history.
    security add-generic-password -U -s "$_tk_service" -a "$v" -w "$_val" >/dev/null && echo "  stored"
    _val=""
  done
  unset _val _opt
}

_tk_setclip() {
  # For terminals where paste does not work: copy a value anywhere (Cmd+C), press Enter here; the value is read from the
  # clipboard with pbpaste, stored in the Keychain, and the clipboard is emptied. Nothing is echoed. Empty clipboard = skip.
  printf '' | pbcopy
  for v in BYBIT_TESTNET_KEY BYBIT_TESTNET_SECRET BINANCE_FUT_TESTNET_KEY BINANCE_FUT_TESTNET_SECRET BINANCE_SPOT_TESTNET_KEY BINANCE_SPOT_TESTNET_SECRET; do
    case " $_tk_optional " in *" $v "*) _opt=" (optional)";; *) _opt="";; esac
    printf 'Copy the value of %s%s, then press Enter (Enter with nothing copied = skip): ' "$v" "$_opt"
    IFS= read -r _go
    _val=$(pbpaste | tr -d '[:space:]')
    if [ -z "$_val" ]; then echo "  nothing on the clipboard, skipped"; continue; fi
    security add-generic-password -U -s "$_tk_service" -a "$v" -w "$_val" >/dev/null && echo "  stored (${#_val} characters)"
    _val=""; printf '' | pbcopy
  done
  unset _val _opt _go
}

_tk_load() {
  _missing=""
  for v in BYBIT_TESTNET_KEY BYBIT_TESTNET_SECRET BINANCE_FUT_TESTNET_KEY BINANCE_FUT_TESTNET_SECRET BINANCE_SPOT_TESTNET_KEY BINANCE_SPOT_TESTNET_SECRET; do
    _val=$(_tk_get "$v")
    if [ -n "$_val" ]; then export "$v=$_val"
    else case " $_tk_required " in *" $v "*) _missing="$_missing $v";; esac; fi
  done
  unset _val
  if [ -n "$_missing" ]; then echo "testnet keys loaded, but missing:$_missing (run: sh testnet_keys.sh set)"
  else echo "testnet keys loaded into this shell"; fi
  unset _missing
}

_tk_check() {
  for v in BYBIT_TESTNET_KEY BYBIT_TESTNET_SECRET BINANCE_FUT_TESTNET_KEY BINANCE_FUT_TESTNET_SECRET BINANCE_SPOT_TESTNET_KEY BINANCE_SPOT_TESTNET_SECRET; do
    _s=$(_tk_get "$v"); eval "_e=\${$v:-}"
    printf '%-28s keychain: %-12s shell: %s\n' "$v" "${_s:+stored(${#_s})}" "${_e:+loaded(${#_e})}"
  done
  unset _s _e
}

case "${1:-}" in
  set)    _tk_set ;;
  setclip) _tk_setclip ;;
  load)   _tk_load ;;
  unload) for v in BYBIT_TESTNET_KEY BYBIT_TESTNET_SECRET BINANCE_FUT_TESTNET_KEY BINANCE_FUT_TESTNET_SECRET BINANCE_SPOT_TESTNET_KEY BINANCE_SPOT_TESTNET_SECRET; do unset "$v"; done; echo "testnet keys removed from this shell" ;;
  check)  _tk_check ;;
  delete) for v in BYBIT_TESTNET_KEY BYBIT_TESTNET_SECRET BINANCE_FUT_TESTNET_KEY BINANCE_FUT_TESTNET_SECRET BINANCE_SPOT_TESTNET_KEY BINANCE_SPOT_TESTNET_SECRET; do security delete-generic-password -s "$_tk_service" -a "$v" >/dev/null 2>&1; done; echo "testnet keys deleted from the Keychain" ;;
  *)      echo "usage: sh testnet_keys.sh set|check|delete   or   . ./testnet_keys.sh load|unload" ;;
esac
