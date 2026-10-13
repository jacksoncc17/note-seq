"""`python -m service.app` runs the service (see `api.main`)."""

from .api import main

if __name__ == '__main__':
  raise SystemExit(main())
