# Test Maintenance Quick Reference

## When You Modify Code, Update Tests!

### Step 1: Check Function Docstring
Every function has a `Tests:` line in its docstring pointing to the test file/class.

Example:
```python
def validate_rip_output(job, db, paths: Optional[JobPaths] = None) -> ValidationResult:
    """
    Validate rip stage output against expected structure.
    
    Tests: tests/test_stage_validation.py::TestRipStageValidation (5 tests)
    Update tests if validation logic, error messages, or function signature changes.
    """
```

### Step 2: Find the Test File
Use the `Tests:` reference to find the test file.

### Step 3: Update Tests
Based on what changed:
- **Function signature changed?** Update test function calls
- **Return value changed?** Update assertions
- **Behavior changed?** Update test expectations
- **New parameters?** Add tests for new parameter behavior

### Step 4: Run Tests
```bash
cd Backend
source .venv/bin/activate
pytest tests/test_<relevant_file>.py -v
```

## Common Modifications

### Adding a Parameter
1. Update function signature
2. Update all test calls to include the parameter
3. Add tests for new parameter behavior

### Changing Return Structure
1. Update function implementation
2. Update all assertions checking the return value
3. Update tests that use the return value

### Changing Validation Logic
1. Update function implementation
2. Update test expectations (errors/warnings/success)
3. Update error message assertions if messages changed

## Full Documentation

See **[TEST_MAINTENANCE_GUIDE.md](./TEST_MAINTENANCE_GUIDE.md)** for complete details.

