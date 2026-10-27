# Local name-only usage integration. No history writes, DEBUG trap, or spool.
# History suppression (including erased duplicates) is a coverage gap: omitted
# records cannot be inferred or recovered. Re-source to install in a new shell.
[[ $- == *i* ]] || return 0
[[ ${__oma_usage_hook_installed-} == 1 ]] && return 0
__oma_usage_hook_installed=1

# Bash's RANDOM is per-process; combine independent draws with the shell PID.
# Neither identity nor sequence contains history source.
printf -v __oma_usage_shell_id '%s-%04x%04x%04x%04x' "$BASHPID" "$RANDOM" "$RANDOM" "$RANDOM" "$RANDOM"
__oma_usage_sequence=0
__oma_usage_history_index=$HISTCMD

__oma_usage_prompt() {
    local __oma_usage_status=$? __oma_usage_current=$HISTCMD
    if [[ :$SHELLOPTS: != *:history:* || ${HISTFILE-} == /dev/null ]]; then
        __oma_usage_history_index=$__oma_usage_current
        return "$__oma_usage_status"
    fi
    ((__oma_usage_sequence += 1))
    if [[ $(command timeout --signal=KILL 0.25s "$HOME/.local/bin/oma-usage" bash-event \
            --session "$__oma_usage_shell_id" --sequence "$__oma_usage_sequence" --check </dev/null 2>/dev/null) != collecting ]]; then
        __oma_usage_history_index=$__oma_usage_current
        return "$__oma_usage_status"
    fi
    if (( __oma_usage_current > __oma_usage_history_index )); then
        # fc is a builtin: source text goes directly through the pipe, never
        # argv, environment, disk, or a shell variable. timeout covers Python
        # startup too; missing collector/helper and parser failures are quiet.
        builtin fc -ln -1 2>/dev/null |
            command timeout --signal=KILL 0.25s "$HOME/.local/bin/oma-usage" bash-event \
                --session "$__oma_usage_shell_id" --sequence "$__oma_usage_sequence" >/dev/null 2>&1
    else
        # An omitted input, empty prompt, or history-index regression must not
        # recount the previous record. Empty input is only a coverage point.
        command timeout --signal=KILL 0.25s "$HOME/.local/bin/oma-usage" bash-event \
            --session "$__oma_usage_shell_id" --sequence "$__oma_usage_sequence" </dev/null >/dev/null 2>&1
    fi
    __oma_usage_history_index=$__oma_usage_current
    return "$__oma_usage_status"
}

__oma_usage_after_prompt() {
    local __oma_usage_status=$?
    # Existing hooks may import another shell's history. Baseline *after* those
    # hooks as well so an omitted next input cannot capture that imported tail.
    __oma_usage_history_index=$HISTCMD
    return "$__oma_usage_status"
}

# Array elements and scalar source remain intact and in their original order.
# The first function returns the incoming status for the original first hook.
if [[ $(declare -p PROMPT_COMMAND 2>/dev/null) == 'declare -a '* ]]; then
    PROMPT_COMMAND=(__oma_usage_prompt "${PROMPT_COMMAND[@]}" __oma_usage_after_prompt)
else
    PROMPT_COMMAND=(__oma_usage_prompt "${PROMPT_COMMAND-}" __oma_usage_after_prompt)
fi
