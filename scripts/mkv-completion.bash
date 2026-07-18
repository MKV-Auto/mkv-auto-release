# mkv command completion for bash
# Installation:
#   System-wide: sudo cp mkv-completion.bash /etc/bash_completion.d/mkv
#   User-only:   mkdir -p ~/.local/share/bash-completion/completions && 
#                cp mkv-completion.bash ~/.local/share/bash-completion/completions/mkv
#   Manual:      source mkv-completion.bash in your ~/.bashrc

_mkv_completion() {
  local cur prev words cword
  _init_completion || return
  
  # First level: mkv [dev|docker|build|test|disc|help]
  if [ $cword -eq 1 ]; then
    COMPREPLY=( $(compgen -W "dev docker build test disc help" -- "$cur") )
    return
  fi
  
  # Second level and beyond: subcommands
  case "${words[1]}" in
    dev)
      _mkv_dev_completion
      ;;
    docker)
      _mkv_docker_completion
      ;;
    build)
      _mkv_build_completion
      ;;
    test)
      _mkv_test_completion
      ;;
    disc)
      _mkv_disc_completion
      ;;
  esac
}

_mkv_dev_completion() {
  local cur prev
  cur="${COMP_WORDS[COMP_CWORD]}"
  prev="${COMP_WORDS[COMP_CWORD-1]}"
  
  # Second level: mkv dev <subcommand>
  if [ $COMP_CWORD -eq 2 ]; then
    COMPREPLY=( $(compgen -W "start stop restart status reset reset-all seed help" -- "$cur") )
    return
  fi
  
  # Third level: depends on subcommand
  if [ $COMP_CWORD -eq 3 ]; then
    case "${COMP_WORDS[2]}" in
      restart)
        COMPREPLY=( $(compgen -W "frontend backend all" -- "$cur") )
        ;;
      seed)
        COMPREPLY=( $(compgen -W "backup create reload" -- "$cur") )
        ;;
    esac
    return
  fi
  
  # Fourth level: seed subcommand options
  if [ $COMP_CWORD -eq 4 ]; then
    case "${COMP_WORDS[2]}" in
      seed)
        case "${COMP_WORDS[3]}" in
          backup|create)
            COMPREPLY=( $(compgen -W "force" -- "$cur") )
            ;;
          reload)
            COMPREPLY=( $(compgen -W "--full full" -- "$cur") )
            ;;
        esac
        ;;
    esac
  fi
}

_mkv_docker_completion() {
  local cur prev
  cur="${COMP_WORDS[COMP_CWORD]}"
  prev="${COMP_WORDS[COMP_CWORD-1]}"
  
  # Second level: mkv docker <subcommand>
  if [ $COMP_CWORD -eq 2 ]; then
    COMPREPLY=( $(compgen -W "start stop restart rebuild reset status logs check watch help" -- "$cur") )
    return
  fi
  
  # Third level: depends on subcommand
  if [ $COMP_CWORD -eq 3 ]; then
    case "${COMP_WORDS[2]}" in
      check)
        COMPREPLY=( $(compgen -W "mkv" -- "$cur") )
        ;;
      rebuild)
        COMPREPLY=( $(compgen -W "frontend backend" -- "$cur") )
        ;;
      logs)
        # Try to get actual log names from container if it's running
        local log_names=""
        if docker ps --filter "name=mkv-auto" --format "{{.Names}}" 2>/dev/null | grep -q "mkv-auto"; then
          # Get application logs
          log_names=$(docker exec mkv-auto sh -c 'ls -1 /data/mkvauto/logs/*.log 2>/dev/null | xargs -n1 basename | sed "s/.log$//"' 2>/dev/null || true)
          # Get supervisor logs
          log_names="$log_names $(docker exec mkv-auto sh -c 'ls -1 /var/log/supervisor/*.log 2>/dev/null | xargs -n1 basename | sed "s/.log$//" | sed "s/_err$//"' 2>/dev/null || true)"
        fi
        # Fallback to common log names (celery = merged; others = per-worker)
        if [ -z "$log_names" ]; then
          log_names="list api uvicorn celery celery-rip celery-postprocess celery-transfer celery-preview celery-extra"
        fi
        COMPREPLY=( $(compgen -W "$log_names list -f --follow --errors -e" -- "$cur") )
        ;;
      watch)
        # For watch, suggest common commands
        if [[ "$cur" == -* ]]; then
          COMPREPLY=( $(compgen -W "-n" -- "$cur") )
        else
          # Suggest some common watch commands
          COMPREPLY=( $(compgen -W "'ps aux' 'df -h' 'supervisorctl status'" -- "$cur") )
        fi
        ;;
    esac
    return
  fi
  
  # Fourth level: log follow options
  if [ $COMP_CWORD -eq 4 ]; then
    case "${COMP_WORDS[2]}" in
      logs)
        # If previous was a log name, suggest follow options
        if [[ "${COMP_WORDS[3]}" != "list" && "${COMP_WORDS[3]}" != "-f" && "${COMP_WORDS[3]}" != "--follow" ]]; then
          COMPREPLY=( $(compgen -W "-f --follow" -- "$cur") )
        fi
        ;;
    esac
  fi
}

_mkv_build_completion() {
  local cur prev
  cur="${COMP_WORDS[COMP_CWORD]}"
  prev="${COMP_WORDS[COMP_CWORD-1]}"
  
  # Second level: mkv build [version]
  if [ $COMP_CWORD -eq 2 ]; then
    # Suggest common version patterns
    if [[ "$cur" == "help" || "$cur" == "-"* ]]; then
      COMPREPLY=( $(compgen -W "help --help -h" -- "$cur") )
    else
      COMPREPLY=( $(compgen -W "latest help" -- "$cur") )
      # If there are git tags, suggest them
      if command -v git >/dev/null 2>&1 && git rev-parse --git-dir >/dev/null 2>&1; then
        local tags=$(git tag -l "v*" 2>/dev/null | tail -5)
        if [ -n "$tags" ]; then
          COMPREPLY+=( $(compgen -W "$tags" -- "$cur") )
        fi
      fi
    fi
  fi
}

_mkv_test_completion() {
  local cur
  cur="${COMP_WORDS[COMP_CWORD]}"
  
  # Second level: mkv test [backend|frontend|e2e|help]
  if [ $COMP_CWORD -eq 2 ]; then
    COMPREPLY=( $(compgen -W "backend frontend e2e makemkv help" -- "$cur") )
  fi
}

_mkv_disc_completion() {
  local cur
  cur="${COMP_WORDS[COMP_CWORD]}"
  
  # Second level: mkv disc <subcommand>
  if [ $COMP_CWORD -eq 2 ]; then
    COMPREPLY=( $(compgen -W "eject inject help" -- "$cur") )
    return
  fi
  
  # Third level: optional drive number (0, 1, 2, ...)
  if [ $COMP_CWORD -eq 3 ] && [[ "${COMP_WORDS[2]}" == "eject" || "${COMP_WORDS[2]}" == "inject" ]]; then
    # Suggest existing /dev/sr* block devices
    local devs=""
    for d in /dev/sr*; do
      [ -b "$d" ] || continue
      devs="$devs ${d#/dev/sr}"
    done
    if [ -n "$devs" ]; then
      COMPREPLY=( $(compgen -W "$devs" -- "$cur") )
    else
      COMPREPLY=( $(compgen -W "0 1 2" -- "$cur") )
    fi
  fi
}

# Register the completion function
complete -F _mkv_completion mkv
