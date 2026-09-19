#compdef misantropic misantropic-backup misantropic-calendar misantropic-contacts misantropic-cookbook misantropic-docs misantropic-gallery misantropic-mail misantropic-mcp misantropic-memory misantropic-notes misantropic-personal misantropic-preset misantropic-research misantropic-sessions misantropic-signature misantropic-skills misantropic-tasks misantropic-theme misantropic-webhook
# Zsh tab-completion for the misantropic umbrella + sub-CLIs.
#
# Drop in any directory on $fpath, e.g.:
#     fpath=(/path/to/odysseus-ui/scripts/_completion $fpath)
#     autoload -U compinit; compinit
#
# Then `misantropic <tab>` completes subcommands; `misantropic mail <tab>`
# completes mail subcommands; `misantropic-mail <tab>` works the same.

_misantropic_scripts_dir() {
    local self="${(%):-%x}"
    while [[ -L "$self" ]]; do self="$(readlink "$self")"; done
    cd "${self:h}/.." && pwd
}

typeset -gA _misantropic_subs

_misantropic_refresh() {
    _misantropic_subs=()
    local dir="$(_misantropic_scripts_dir)"
    local py="$dir/../venv/bin/python"
    [[ -x "$py" ]] || py="$(command -v python3)"
    local f sub help_out commands
    for f in "$dir"/misantropic-*; do
        [[ -x "$f" ]] || continue
        case "$f" in
            *.bak|*.pyc|*.pre-*) continue ;;
        esac
        sub="${${f:t}#misantropic-}"
        help_out=$("$py" "$f" --help 2>/dev/null) || continue
        commands=$(echo "$help_out" | grep -oE '\{[a-z0-9_,-]+\}' | head -1 \
            | tr -d '{}' | tr ',' ' ')
        _misantropic_subs[$sub]="$commands"
    done
}

_misantropic() {
    [[ ${#_misantropic_subs} -eq 0 ]] && _misantropic_refresh

    local cmd="${words[1]}"

    if [[ "$cmd" == "misantropic" ]]; then
        if (( CURRENT == 2 )); then
            local -a subs=(${(k)_misantropic_subs} help)
            _describe 'subcommand' subs
            return
        fi
        local sub="${words[2]}"
        if [[ "$sub" == "help" ]] && (( CURRENT == 3 )); then
            local -a subs=(${(k)_misantropic_subs})
            _describe 'subcommand' subs
            return
        fi
        if (( CURRENT == 3 )); then
            local -a sc=(${(s/ /)_misantropic_subs[$sub]})
            _describe 'command' sc
            return
        fi
        return
    fi

    # misantropic-foo <tab>
    local sub="${cmd#misantropic-}"
    if (( CURRENT == 2 )); then
        local -a sc=(${(s/ /)_misantropic_subs[$sub]})
        _describe 'command' sc
        return
    fi
}

_misantropic "$@"
