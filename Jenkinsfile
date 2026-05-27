pipeline {
    agent any

    environment {
        PATH = "/usr/local/bin:/opt/homebrew/bin:${env.PATH}"
        DOCKER_IMAGE = "amirdehdari/flask-mongo-app"
        IMAGE_TAG    = "${BUILD_NUMBER}"
        KUBECONFIG   = "${HOME}/.kube/config"
    }

    stages {


        stage('Build Docker Image') {
            steps {
                sh "docker build -t ${DOCKER_IMAGE}:${IMAGE_TAG} ."
            }
        }

        stage('Push to Docker Hub') {
            steps {
                withCredentials([usernamePassword(
                    credentialsId: 'dockerhub-cred',
                    usernameVariable: 'DOCKER_USER',
                    passwordVariable: 'DOCKER_PASS'
                )]) {
                    sh "echo ${DOCKER_PASS} | docker login -u ${DOCKER_USER} --password-stdin"
                    sh "docker push ${DOCKER_IMAGE}:${IMAGE_TAG}"
                }
            }
        }

        stage('Deploy to Kubernetes') {
            steps {
                sh "kubectl set image deployment/flask-app flask-app=${DOCKER_IMAGE}:${IMAGE_TAG} -n app"
                sh "kubectl rollout status deployment/flask-app --timeout=60s"
            }
        }
    }

    post {
        success {
            sh "echo 'Deployed ${DOCKER_IMAGE}:${IMAGE_TAG} successfully'"
        }
        failure {
            sh "echo 'Pipeline failed!'"
        }
        always {
            sh "docker logout || true"
        }
    }
}
