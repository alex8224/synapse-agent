from synapse.tool_output import ContentType, TransformResult


class FixtureTransformer:
    name = "fixture-json-v1"
    content_types = frozenset({ContentType.JSON})

    def transform(self, content, context):
        return TransformResult(
            content="fixture compressed",
            transformer=self.name,
            content_type=ContentType.JSON,
            critical_total=0,
            critical_retained=0,
        )


fixture_transformer = FixtureTransformer()
