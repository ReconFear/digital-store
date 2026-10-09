from fastapi.testclient import TestClient
import api


def test_orders_list_is_not_public():
    client = TestClient(api.app)

    response = client.get("/orders")

    assert response.status_code == 403
    assert response.json()["detail"] == "Order listing is not public"