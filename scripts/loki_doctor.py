"""loki doctor from a checkout: python scripts/loki_doctor.py [--offline] [--show-token].
The checks live in agent/doctor.py so the same code runs inside the container."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "agent"))

import doctor  # noqa: E402

if __name__ == "__main__":
    sys.exit(doctor.main())
