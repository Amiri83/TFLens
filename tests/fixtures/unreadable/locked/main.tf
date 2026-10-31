# The tests copy this fixture and make locked/ unreadable (chmod 000).
resource "aws_sns_topic" "hidden" {
  name = "repo-a-hidden"
}
