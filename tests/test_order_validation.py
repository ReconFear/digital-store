from fastapi.testclient import TestClient
import api


def test_order_negative_product_id():
    client = TestClient(api.app)

    response = client.post(
        "/orders",
        json={"product_id": -5}
    )

    assert response.status_code == 422
    assert response.json()["detail"][0]["type"] == "greater_than"