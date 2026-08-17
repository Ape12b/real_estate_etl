import logging
import azure.functions as func

app = func.FunctionApp()



@app.route(
    route="parse_csv",
    methods=["POST"],
    auth_level=func.AuthLevel.FUNCTION,
)
def parse_csv(req: func.HttpRequest) -> func.HttpResponse:
    logging.info("Airflow called the ETL function.")

    blob_name = req.params.get("blob_name")

    if not blob_name:
        try:
            body = req.get_json()
            blob_name = body.get("blob_name")
        except ValueError:
            blob_name = None

    if not blob_name:
        return func.HttpResponse(
            "Missing blob_name",
            status_code=400,
        )

    logging.info(f"Received blob: {blob_name}")

    return func.HttpResponse(
        f"ETL function received blob: {blob_name}",
        status_code=200,
    )