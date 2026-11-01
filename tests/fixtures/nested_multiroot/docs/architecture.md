# Architecture

Jobs are queued on an `aws_sqs_queue` created by the shared `modules/queue` module.
Alarms are sent to the `aws_sns_topic.alerts` topic.
