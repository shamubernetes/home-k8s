def main(data):
    return data.get("bootstrap_cloudflare", {}).get("enabled", False) is True
