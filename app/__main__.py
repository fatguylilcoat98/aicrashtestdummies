from app.config import Config
from app.server import App, serve


def main() -> None:
    config = Config.from_env()
    if config.ephemeral_secret:
        # The value is not printed. A restart drops every session.
        print("ACTD session secret is ephemeral. Set ACTD_SESSION_SECRET before a real deploy.")
    app = App(config)
    serve(app)


if __name__ == "__main__":
    main()
