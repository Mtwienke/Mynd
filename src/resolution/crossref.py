import httpx


class CrossrefClient:
    BASE_URL = "https://api.crossref.org"

    def __init__(self, email: str):
        self.client = httpx.Client(
            base_url=self.BASE_URL,
            params={"mailto": email},
            timeout=20.0,
        )

    def find_by_doi(self, doi: str) -> dict | None:
        doi = doi.strip()
        doi = doi.removeprefix("https://doi.org/")
        doi = doi.removeprefix("http://doi.org/")

        response = self.client.get(
            f"/works/{doi}"
        )

        if response.status_code == 404:
            return None

        response.raise_for_status()
        return response.json()["message"]

    def close(self):
        self.client.close()