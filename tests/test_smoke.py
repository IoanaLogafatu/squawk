def test_ingestor_imports():
    """
    Ensures the personal_adsb ingestor module and its 
    dependencies (requests, schemas, config) load correctly.
    """
    from ingestor.personal_adsb.ingestor import run, SOURCE_NAME
    
    assert SOURCE_NAME == "PersonalADSB"
    assert callable(run)
