"""
Path template resolution system for dynamic transfer destination paths.

Path template schema version: changes to the set of available variables
(get_available_variables) or to resolution rules (resolve_template) must bump
PATH_TEMPLATE_SCHEMA_VERSION so that stored configs and logs remain traceable.
"""
import re
from typing import Dict, List, Tuple, Any

# Bump when adding/removing variables or changing resolution behavior.
PATH_TEMPLATE_SCHEMA_VERSION = "1"


def get_available_variables() -> List[str]:
    """List all available template variables."""
    return [
        "movie_name",
        "movie_year",
        "year",
        "release_name",
        "release_year",
        "release_slug",
        "disc_number",
        "disc_name",
        "type",
        "format",
    ]


def validate_template(template: str) -> Tuple[bool, str]:
    """
    Validate template syntax.
    Returns (is_valid, error_message).
    """
    if not template:
        return True, ""
    
    # Check for balanced braces
    open_braces = template.count("{")
    close_braces = template.count("}")
    if open_braces != close_braces:
        return False, "Unbalanced braces in template"
    
    # Check for properly closed braces (no unclosed { before a })
    brace_stack = []
    paren_stack = []
    for i, char in enumerate(template):
        if char == "{":
            brace_stack.append(i)
        elif char == "}":
            if not brace_stack:
                return False, "Unbalanced braces in template"
            brace_stack.pop()
        elif char == "(":
            paren_stack.append(i)
        elif char == ")":
            if paren_stack:
                paren_stack.pop()
    
    if brace_stack:
        return False, "Unbalanced braces in template"
    
    # Check that braces inside parentheses are closed before the paren closes
    # This catches cases like "{movie_name} ({year}" where {year} is inside () but ) is missing
    if paren_stack:
        # There are unclosed parentheses - check if any braces are inside them
        for paren_start in paren_stack:
            # Find braces that start after this paren
            for brace_pos in [i for i, c in enumerate(template) if c == "{" and i > paren_start]:
                # Check if this brace is closed before the template ends
                brace_close = template.find("}", brace_pos + 1)
                if brace_close == -1:
                    return False, "Unbalanced braces in template"
                # If there's a paren after the brace but no closing paren, it's invalid
                if template.find(")", brace_close) == -1 and paren_start < brace_pos:
                    return False, "Unbalanced braces in template"
    
    # Check for valid variable names - must match complete {var} patterns
    available_vars = get_available_variables()
    pattern = r"\{([^}]+)\}"
    matches = re.findall(pattern, template)
    
    # Also check for any { that isn't followed by a } before another {
    # This catches cases like {var{ or {var without closing }
    for i in range(len(template)):
        if template[i] == "{":
            # Find the next } or { (whichever comes first)
            next_close = template.find("}", i + 1)
            next_open = template.find("{", i + 1)
            if next_close == -1 or (next_open != -1 and next_open < next_close):
                return False, "Unbalanced braces in template"
    
    for var in matches:
        if var not in available_vars:
            return False, f"Unknown variable: {var}"
    
    return True, ""


def resolve_template(template: str, context: Dict[str, Any]) -> str:
    """
    Resolve a path template with the given context.
    
    Args:
        template: Template string with variables like {movie_name}, {year}, etc.
        context: Dictionary with variable values
        
    Returns:
        Resolved path string
    """
    if not template:
        return ""
    
    # Normalize variable names (handle both movie_year and year)
    resolved_context = {}
    for key, value in context.items():
        resolved_context[key] = value
        if key == "movie_year":
            resolved_context["year"] = value
    
    # Format disc_number as zero-padded 2-digit string
    if "disc_number" in resolved_context and resolved_context["disc_number"] is not None:
        try:
            disc_num = int(resolved_context["disc_number"])
            resolved_context["disc_number"] = f"{disc_num:02d}"
        except (ValueError, TypeError):
            resolved_context["disc_number"] = str(resolved_context["disc_number"])
    
    # Replace variables in template
    result = template
    available_vars = get_available_variables()
    
    for var in available_vars:
        pattern = f"{{{var}}}"
        value = resolved_context.get(var)
        if value is not None:
            # Sanitize value for filesystem (remove invalid characters)
            sanitized = str(value).replace("/", "-").replace("\\", "-").strip()
            result = result.replace(pattern, sanitized)
        else:
            # Replace with empty string if variable not found
            result = result.replace(pattern, "")
    
    # Clean up any double slashes
    result = re.sub(r"/+", "/", result)
    
    # Preserve trailing slash if template ends with a variable or slash
    # This handles cases like "{movie_name}/{release_name}" where release_name is empty
    template_ends_with_var_or_slash = template.rstrip().endswith("/") or re.search(r"\{[^}]+\}\s*$", template)
    if template_ends_with_var_or_slash and result.endswith("/"):
        # Keep the trailing slash
        pass
    elif not template.endswith("/"):
        # Strip trailing slash if template didn't end with one
        result = result.rstrip("/")
    
    return result

