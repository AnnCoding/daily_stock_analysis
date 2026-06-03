"""Generate Longbridge Access Token via OAuth 2.0 flow."""
import webbrowser
from longbridge.openapi import Config, OAuthBuilder

CLIENT_ID = "c4172947-7232-411d-95df-8e4166a76bc3"


def main():
    print("Starting Longbridge OAuth 2.0 authorization...")

    def on_url(url: str):
        print(f"\nAuthorization URL: {url}\n")
        print("Opening browser for authorization...")
        webbrowser.open(url)

    oauth = OAuthBuilder(CLIENT_ID).build(on_url)
    config = Config.from_oauth(oauth)

    # Verify connection by creating a QuoteContext
    from longbridge.openapi import QuoteContext
    ctx = QuoteContext(config)

    # Try fetching a quote to verify
    resp = ctx.quote(["700.HK"])
    print("\nAuthorization successful!")
    print(f"Test quote for 700.HK: {resp}")

    print(f"\nOAuth token saved to: ~/.longbridge/openapi/tokens/{CLIENT_ID}")
    print("Token will be automatically refreshed when expired.")


if __name__ == "__main__":
    main()
