from fastapi.testclient import TestClient
import api


def test_create_order_unavailable(monkeypatch):
    def fake_create_order(product_id):
        return {"error": "unavailable"}

    monkeypatch.setattr(
        api,
        "create_order_in_db",
        fake_create_order
    )

    client = TestClient(api.app)

    response = client.post(
        "/orders",
        json={"product_id": 2}
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "Product unavailable"