def test_the_worker_starts_without_the_webhook_secret_it_never_reads() -> None:
    """The deployment withholds ``RAZORPAY_WEBHOOK_SECRET`` from the worker, and it must.

    The worker talks to Razorpay outbound and never receives a delivery; verifying a webhook
    is the API's job. So ``infra/terraform/locals.tf`` and ``csi-sm.yaml`` grant the worker
    four secrets and not that one -- least privilege, exactly as ADR 0003 D3 intends. Until
    this test existed the settings demanded it anyway, so the worker refused to start under
    its own manifest: ``ValidationError: RAZORPAY_WEBHOOK_SECRET Field required``, a crash
    loop in the only process that moves money. A deployment verified end to end would have
    come up with no payment ever executing.

    Absent is allowed. Blank is not -- ``test_razorpay_config`` still proves that -- because
    absent and blank are different facts about a deployment.
    """
    from action_executor.settings import WorkerSettings

    # Literal fakes, not conftest's: the two URLs only need to be non-blank and different,
    # and the key is prefix-checked and never dialled. Nothing here can reach a real
    # database or a real provider.
    key_id = "rzp_test_dwkworkerkey"
    settings = WorkerSettings(
        PROFILE="development",
        DATABASE_URL_WORKER="postgresql+psycopg://w:w@localhost/never",
        DATABASE_URL_KERNEL="postgresql+psycopg://k:k@localhost/never",
        RAZORPAY_KEY_ID=key_id,
        RAZORPAY_KEY_SECRET="dwk-test-api-secret",  # noqa: S106 - fake, prefix-checked only
        WORKER_ID="dwk-test-worker",
    )
    assert settings.razorpay_webhook_secret is None
    razorpay = settings.razorpay()
    assert razorpay.webhook_secret is None
    assert razorpay.key_id == key_id
